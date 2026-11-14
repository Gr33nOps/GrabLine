"""PC certificate, key storage and the pin (PROTOCOL.md section 4)."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core import paths
from app.remote import certs

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "protocol" / "fixtures" / "vectors.json").read_text(
        encoding="utf-8"
    )
)
PC_ID = VECTORS["pcId"]
OTHER_PC_ID = "AAAAAAAAAAAAAAAAAAAAAA"


def _cert(identity: certs.PcIdentity) -> x509.Certificate:
    return x509.load_pem_x509_certificate(identity.cert_pem)


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_creates_p256_self_signed_certificate(tmp_path):
    identity = certs.load_or_create(PC_ID, tmp_path)
    cert = _cert(identity)
    key = serialization.load_pem_private_key(identity.key_pem, password=None)

    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert isinstance(key.curve, ec.SECP256R1)
    assert cert.version is x509.Version.v3
    assert cert.subject == cert.issuer
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == f"GrabLine {PC_ID}"
    assert len(cert.extensions) == 0  # no SubjectAltName
    cert.verify_directly_issued_by(cert)

    now = dt.datetime.now(dt.UTC)
    assert dt.timedelta(hours=23) < now - cert.not_valid_before_utc < dt.timedelta(hours=25)
    years = (cert.not_valid_after_utc - now).days / 365.25
    assert 29.9 < years < 30.1

    assert (tmp_path / certs.CERT_FILENAME).read_bytes() == identity.cert_pem
    assert (tmp_path / certs.KEY_FILENAME).read_bytes() == identity.key_pem
    assert identity.pin == certs.pin(cert)
    assert len(identity.pin) == 43
    assert sorted(p.name for p in tmp_path.iterdir()) == [certs.CERT_FILENAME, certs.KEY_FILENAME]


def test_default_location_is_remote_folder_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "Grabline")
    certs.load_or_create(PC_ID)
    assert certs.remote_dir() == tmp_path / "Grabline" / "remote"
    assert (tmp_path / "Grabline" / "remote" / "remote_key.pem").is_file()
    assert (tmp_path / "Grabline" / "remote" / "remote_cert.pem").is_file()


def test_reused_on_a_fresh_load(tmp_path):
    first = certs.load_or_create(PC_ID, tmp_path)
    second = certs.load_or_create(PC_ID, tmp_path)
    assert second == first


def test_regenerate_changes_the_pin(tmp_path):
    first = certs.load_or_create(PC_ID, tmp_path)
    second = certs.regenerate(PC_ID, tmp_path)
    assert second.pin != first.pin
    assert second.key_pem != first.key_pem
    assert certs.load_or_create(PC_ID, tmp_path) == second


@pytest.mark.parametrize(
    "damage",
    [
        lambda p: p.write_bytes(b"-----BEGIN EC PRIVATE KEY-----\nnot a key\n"),
        lambda p: p.write_bytes(b""),
        lambda p: p.write_bytes(b"A" * (certs._MAX_PEM_BYTES + 1)),
        lambda p: p.unlink(),
        lambda p: p.write_bytes(
            ec.generate_private_key(ec.SECP384R1()).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        ),
    ],
    ids=["garbage", "empty", "oversized", "missing", "wrong-curve"],
)
def test_unusable_key_regenerates_with_a_new_pin(tmp_path, damage):
    first = certs.load_or_create(PC_ID, tmp_path)
    damage(tmp_path / certs.KEY_FILENAME)
    second = certs.load_or_create(PC_ID, tmp_path)
    assert second.pin != first.pin
    assert second.pin == certs.pin(_cert(second))
    assert certs.load_or_create(PC_ID, tmp_path) == second


def test_certificate_for_another_key_is_replaced_and_the_key_wins(tmp_path):
    first = certs.load_or_create(PC_ID, tmp_path)
    other = certs.load_or_create(PC_ID, tmp_path / "other")
    (tmp_path / certs.CERT_FILENAME).write_bytes(other.cert_pem)
    second = certs.load_or_create(PC_ID, tmp_path)
    assert second.pin == first.pin == certs.pin(_cert(second))
    assert second.key_pem == first.key_pem


@pytest.mark.parametrize("damage", ["garbage", "missing"])
def test_bad_certificate_is_reissued_around_the_same_key(tmp_path, damage):
    first = certs.load_or_create(PC_ID, tmp_path)
    cert_path = tmp_path / certs.CERT_FILENAME
    if damage == "garbage":
        cert_path.write_bytes(b"-----BEGIN CERTIFICATE-----\nnope\n")
    else:
        cert_path.unlink()
    second = certs.load_or_create(PC_ID, tmp_path)
    assert second.pin == first.pin
    assert second.key_pem == first.key_pem
    assert second.cert_pem != first.cert_pem
    assert cert_path.read_bytes() == second.cert_pem


def test_changed_pc_id_reissues_the_certificate_and_keeps_the_pin(tmp_path):
    first = certs.load_or_create(PC_ID, tmp_path)
    second = certs.load_or_create(OTHER_PC_ID, tmp_path)
    assert second.pin == first.pin
    cn = _cert(second).subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == f"GrabLine {OTHER_PC_ID}"


@pytest.mark.parametrize("bad", ["", "short", "A" * 23, "A" * 21 + "/", "A" * 21 + "\n"])
def test_bad_pc_id_is_refused_and_nothing_is_written(tmp_path, bad):
    target = tmp_path / "remote"
    with pytest.raises(ValueError):
        certs.load_or_create(bad, target)
    with pytest.raises(ValueError):
        certs.regenerate(bad, target)
    assert not target.exists()


def test_pin_matches_the_protocol_vectors():
    # The fixture's stand-in SPKI.
    assert certs.pin_from_spki(b"grabline-test-pc-spki") == VECTORS["pinnedFingerprint"]
    assert _b64url(VECTORS["pinnedFingerprint"]).hex() == VECTORS["pinnedFingerprintHex"]
    # A real P-256 SPKI from the fixtures.
    spki = _b64url(VECTORS["deviceKey"]["publicKeySpkiDer"])
    expected_hex = VECTORS["deviceKey"]["publicKeySpkiDerSha256Hex"]
    assert _b64url(certs.pin_from_spki(spki)).hex() == expected_hex


def test_pin_is_the_hash_of_the_certificate_spki(tmp_path):
    identity = certs.load_or_create(PC_ID, tmp_path)
    spki = (
        _cert(identity)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    expected = base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).decode().rstrip("=")
    assert identity.pin == expected
    assert "=" not in identity.pin


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_key_file_and_folder_are_owner_only(tmp_path):
    target = tmp_path / "remote"
    certs.load_or_create(PC_ID, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / certs.KEY_FILENAME).stat().st_mode) == 0o600
    certs.regenerate(PC_ID, target)
    assert stat.S_IMODE((target / certs.KEY_FILENAME).stat().st_mode) == 0o600


def test_failed_write_leaves_the_old_identity_and_no_temp_file(tmp_path, monkeypatch):
    first = certs.load_or_create(PC_ID, tmp_path)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        certs.regenerate(PC_ID, tmp_path)
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == [certs.CERT_FILENAME, certs.KEY_FILENAME]
    assert certs.load_or_create(PC_ID, tmp_path) == first


def test_no_secrets_in_logs_or_repr(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    first = certs.load_or_create(PC_ID, tmp_path)
    (tmp_path / certs.KEY_FILENAME).write_bytes(b"junk")
    second = certs.load_or_create(PC_ID, tmp_path)
    third = certs.regenerate(PC_ID, tmp_path)
    assert caplog.records  # the events themselves are logged
    for identity in (first, second, third):
        body = "".join(identity.key_pem.decode().splitlines()[1:-1])
        assert "PRIVATE KEY" not in caplog.text
        assert body[:24] not in caplog.text
        assert identity.pin not in caplog.text
        assert "PRIVATE KEY" not in repr(identity)
        assert body[:24] not in repr(identity)


def test_qt_objects_load(tmp_path):
    from PySide6.QtNetwork import QSsl, QSslSocket

    if not QSslSocket.supportsSsl():
        pytest.skip("this Qt build has no TLS backend (QSslSocket.supportsSsl() is false)")
    assert certs.qt_tls_available() is True

    identity = certs.load_or_create(PC_ID, tmp_path)
    qt_cert = certs.qt_certificate(identity)
    qt_key = certs.qt_private_key(identity)

    assert not qt_cert.isNull()
    assert not qt_key.isNull()
    assert qt_key.algorithm() == QSsl.KeyAlgorithm.Ec
    assert qt_key.type() == QSsl.KeyType.PrivateKey
    assert bytes(qt_cert.toDer().data()) == _cert(identity).public_bytes(serialization.Encoding.DER)
    # Qt sees the same public key the phone pins.
    spki = bytes(qt_cert.publicKey().toDer().data())
    assert certs.pin_from_spki(spki) == identity.pin

    bad = certs.PcIdentity(b"not a cert", b"not a key", identity.pin)
    with pytest.raises(ValueError):
        certs.qt_certificate(bad)
    with pytest.raises(ValueError):
        certs.qt_private_key(bad)

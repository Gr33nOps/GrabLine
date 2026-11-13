"""Real torrent metadata remains usable without deprecated libtorrent APIs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.engines.torrent import _lt, create_torrent_file, parse_torrent

pytestmark = pytest.mark.filterwarnings("error::DeprecationWarning")


def test_create_single_file_preserves_payload_and_web_seed(tmp_path: Path):
    source = tmp_path / "clip.bin"
    source.write_bytes(b"abc")
    data = create_torrent_file(source, web_seeds=("https://seed.example/clip.bin",))
    decoded = _lt().bdecode(data)
    assert decoded[b"info"][b"name"] == b"clip.bin"
    assert _lt().load_torrent_buffer(data).url_seeds == ["https://seed.example/clip.bin"]
    assert parse_torrent(data).total_size == 3


def test_parse_v1_metadata_preserves_tracker_order_without_deprecated_accessors():
    data = _lt().bencode(
        {
            b"announce": b"https://primary.example/announce",
            b"announce-list": [
                [b"https://primary.example/announce"],
                [b"https://backup.example/announce"],
            ],
            b"comment": b"QA fixture",
            b"info": {
                b"name": b"clip.bin",
                b"length": 3,
                b"piece length": 16384,
                b"pieces": hashlib.sha1(b"abc").digest(),
            },
        }
    )
    meta = parse_torrent(data)
    assert meta.name == "clip.bin"
    assert [(f.path, f.size) for f in meta.files] == [("clip.bin", 3)]
    assert meta.comment == "QA fixture"
    assert meta.trackers == (
        "https://primary.example/announce",
        "https://backup.example/announce",
    )

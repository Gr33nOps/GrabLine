"""The torrent client: creation/parsing, magnet helpers, resolver routing,
RSS feed parsing, settings, and a real loopback seed -> download transfer
through the manager (no mocks - actual libtorrent peers on 127.0.0.1).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core import rss
from app.core.errors import DownloadError
from app.core.manager import DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.resolver import Resolver
from app.core.settings import Settings
from app.db.database import Database
from app.engines.torrent import (
    create_torrent_file,
    fetch_torrent_bytes,
    is_torrent_source,
    magnet_display_name,
    magnet_from_torrent,
    parse_torrent,
)
from app.tests.test_resolver import FakeSmart

# ------------------------------------------------------------ create + parse


def test_create_and_parse_roundtrip(tmp_path: Path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.bin").write_bytes(b"A" * 50_000)
    (share / "b.bin").write_bytes(b"B" * 30_000)
    data = create_torrent_file(
        share, trackers=("http://tr.example/announce",), comment="hello", private=True
    )
    meta = parse_torrent(data)
    assert meta.name == "share"
    assert meta.total_size == 80_000  # pad files don't count
    assert [(f.path, f.size) for f in meta.files] == [
        ("share/a.bin", 50_000),
        ("share/b.bin", 30_000),
    ]
    assert meta.comment == "hello"
    assert meta.trackers == ("http://tr.example/announce",)


def test_priorities_skip_by_real_index(tmp_path: Path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.bin").write_bytes(b"A" * 50_000)
    (share / "b.bin").write_bytes(b"B" * 30_000)
    meta = parse_torrent(create_torrent_file(share))
    skipped = {meta.files[0].index}
    priorities = meta.priorities_for(skipped)
    assert priorities[meta.files[0].index] == 0
    assert priorities[meta.files[1].index] == 4


def test_magnet_from_torrent(tmp_path: Path):
    payload = tmp_path / "file.bin"
    payload.write_bytes(b"x" * 20_000)
    magnet = magnet_from_torrent(create_torrent_file(payload))
    assert magnet.startswith("magnet:?xt=urn:btih:")


def test_parse_rejects_junk():
    with pytest.raises(DownloadError, match="not a valid torrent"):
        parse_torrent(b"this is not bencoded")


def test_source_detection_and_magnet_name():
    assert is_torrent_source("magnet:?xt=urn:btih:abc")
    assert not is_torrent_source("magnet:?dn=no-hash")
    assert is_torrent_source("https://x.example/file.torrent?token=1")
    assert is_torrent_source("/home/me/file.torrent")
    assert not is_torrent_source("https://x.example/file.mp4")
    assert magnet_display_name("magnet:?xt=urn:btih:abc&dn=My%20Show") == "My Show"
    assert magnet_display_name("magnet:?xt=urn:btih:abc") is None


def test_fetch_torrent_bytes_missing_file(tmp_path: Path):
    with pytest.raises(DownloadError, match="not found"):
        fetch_torrent_bytes(str(tmp_path / "gone.torrent"))


# ------------------------------------------------------------------ resolver


def test_resolver_routes_magnets_and_torrent_urls():
    resolver = Resolver(FakeSmart(match=False))
    assert resolver.resolve("magnet:?xt=urn:btih:abc").kind is JobKind.TORRENT
    assert resolver.resolve("https://x.example/linux.torrent").kind is JobKind.TORRENT
    refused = resolver.resolve("gopher://host/file")
    assert refused.kind is None and "magnet" in (refused.message or "")


# ----------------------------------------------------------------- settings


def test_torrent_settings_roundtrip(db: Database):
    settings = Settings(db)
    assert settings.torrent_port == 6881
    assert settings.torrent_dht is True
    assert settings.torrent_seed is True
    assert settings.torrent_ratio_limit == 2.0
    settings.torrent_port = 7000
    settings.torrent_ratio_limit = 1.5
    settings.torrent_upload_kbps = 256
    settings.torrent_sequential = True
    settings.rss_feeds = ["https://feed.example/rss | linux"]
    fresh = Settings(db)
    assert fresh.torrent_port == 7000
    assert fresh.torrent_ratio_limit == 1.5
    assert fresh.torrent_upload_kbps == 256
    assert fresh.torrent_sequential is True
    assert fresh.rss_feeds == ("https://feed.example/rss | linux",)


def test_ratio_ticker_survives_closed_database(db: Database, tmp_path: Path):
    """Manager.shutdown must detach Settings from the process-wide torrent
    session so the daemon ratio thread cannot hit a closed SQLite connection
    (the PytestUnhandledThreadExceptionWarning that used to fire on teardown)."""
    from app.engines.torrent import SESSION

    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        SESSION.configure(settings)
        assert SESSION._settings is settings
    finally:
        manager.shutdown()
    assert SESSION._settings is None
    # Fixture closes ``db`` after this; with detach the ticker cannot touch it.


def test_rss_seen_is_capped(db: Database):
    settings = Settings(db)
    settings.rss_seen = [f"guid-{i}" for i in range(600)]
    assert len(Settings(db).rss_seen) == 500
    assert Settings(db).rss_seen[-1] == "guid-599"


# ---------------------------------------------------------------------- rss


_RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Releases</title>
<item><title>Linux ISO weekly</title>
  <enclosure url="https://mirror.example/linux.torrent" type="application/x-bittorrent"/>
  <guid>rel-1</guid></item>
<item><title>Podcast episode</title><link>https://site.example/ep2</link><guid>rel-2</guid></item>
</channel></rss>"""

_ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>
<entry><title>Nightly build</title>
  <link rel="enclosure" href="magnet:?xt=urn:btih:abc&amp;dn=nightly"/>
  <id>atom-1</id></entry>
</feed>"""


def test_rss_parsing_prefers_torrent_enclosures():
    items = rss.parse_feed(_RSS_XML)
    assert items[0].title == "Linux ISO weekly"
    assert items[0].link == "https://mirror.example/linux.torrent"
    assert items[0].guid == "rel-1"
    assert items[1].link == "https://site.example/ep2"


def test_atom_parsing_and_filtering():
    items = rss.parse_feed(_ATOM_XML)
    assert items[0].link.startswith("magnet:")
    assert rss.matching_items(items, "nightly")[0].guid == "atom-1"
    assert rss.matching_items(items, "no-match") == []
    assert rss.parse_feed_line("https://f.example/rss | linux") == (
        "https://f.example/rss",
        "linux",
    )


def test_rss_rejects_junk():
    with pytest.raises(DownloadError, match="not a valid feed"):
        rss.parse_feed("<not-xml")


# --------------------------------------------------------------- end to end


def test_torrent_loopback_transfer(db: Database, tmp_path: Path):
    """The real thing: a libtorrent seeder on 127.0.0.1 and Grabline's manager
    downloading from it - metadata naming, progress, and a byte-perfect file."""
    lt = pytest.importorskip("libtorrent")

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    payload = bytes(range(256)) * 1000  # 256 KB
    (seed_dir / "payload.bin").write_bytes(payload)
    torrent_bytes = create_torrent_file(seed_dir / "payload.bin")
    torrent_path = tmp_path / "payload.torrent"
    torrent_path.write_bytes(torrent_bytes)

    seeder = lt.session(
        {
            "listen_interfaces": "127.0.0.1:6971",
            "enable_dht": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
        }
    )
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(lt.bdecode(torrent_bytes))
    params.save_path = str(seed_dir)
    seed_handle = seeder.add_torrent(params)
    deadline = time.time() + 30
    while time.time() < deadline and not seed_handle.status().is_seeding:
        time.sleep(0.2)
    assert seed_handle.status().is_seeding

    settings = Settings(db)
    settings.torrent_port = 6972
    settings.torrent_dht = False
    settings.torrent_upnp = False
    settings.torrent_natpmp = False
    settings.torrent_seed = False
    dest = tmp_path / "dl"
    dest.mkdir()
    manager = DownloadManager(db, settings=settings, max_concurrent=1)
    try:
        job = manager.add_torrent(
            str(torrent_path), dest_dir=dest, options={"peers": ["127.0.0.1:6971"]}
        )
        assert job.kind is JobKind.TORRENT
        deadline = time.time() + 60
        while time.time() < deadline:
            fresh = db.get_job(job.id)
            assert fresh is not None
            if fresh.status is JobStatus.COMPLETED:
                break
            time.sleep(0.5)
        fresh = db.get_job(job.id)
        assert fresh is not None and fresh.status is JobStatus.COMPLETED
        assert fresh.filename == "payload.bin"  # named from metadata
        assert fresh.total_size == len(payload)
        assert (dest / "payload.bin").read_bytes() == payload
        # The run records the info-hash, so the detail panel's Peers tab can
        # resolve this torrent's live swarm stats from the session.
        assert fresh.options.get("info_hash")
        stats = manager.torrent_stats(job.id)
        assert stats is not None
        assert stats.downloaded >= len(payload) and stats.uploaded >= 0
    finally:
        manager.shutdown()


def test_torrent_stats_guards(db: Database, tmp_path: Path):
    """torrent_stats returns None (never raises, never touches the session) for
    a non-torrent job or a torrent that has no recorded info-hash yet."""
    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        plain = db.create_job("http://x.example/y.bin", str(tmp_path), "y.bin")
        assert manager.torrent_stats(plain.id) is None
        torrent = db.create_job(
            "magnet:?xt=urn:btih:" + "a" * 40, str(tmp_path), "t", kind=JobKind.TORRENT
        )
        assert manager.torrent_stats(torrent.id) is None  # no info-hash recorded
        assert manager.torrent_stats(999_999) is None  # no such job
    finally:
        manager.shutdown()

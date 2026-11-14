"""A completed torrent must be readable immediately, including while seeding."""

from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path

import pytest

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.engines.torrent import SESSION, _lt, create_torrent_file, magnet_from_torrent
from app.tests.network_ports import torrent_port


@pytest.mark.parametrize("magnet", [False, True])
@pytest.mark.parametrize("seed_after", [False, True])
def test_completed_torrent_bytes_are_already_on_disk(
    db: Database, tmp_path: Path, magnet: bool, seed_after: bool
) -> None:
    lt = _lt()
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    data = random.Random(423 + int(magnet) + int(seed_after) * 2).randbytes(8 * 1024**2)
    (seed_dir / "payload.bin").write_bytes(data)
    expected = hashlib.sha256(data).digest()
    torrent = create_torrent_file(seed_dir / "payload.bin")
    torrent_path = tmp_path / "payload.torrent"
    torrent_path.write_bytes(torrent)
    seeder = lt.session(
        {
            "listen_interfaces": f"127.0.0.1:{torrent_port()}",
            "enable_dht": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
        }
    )
    params = lt.load_torrent_buffer(torrent)
    params.save_path = str(seed_dir)
    seed_handle = seeder.add_torrent(params)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not seed_handle.status().is_seeding:
        time.sleep(0.01)
    assert seed_handle.status().is_seeding
    settings = Settings(db)
    settings.torrent_port = torrent_port()
    settings.torrent_dht = settings.torrent_upnp = settings.torrent_natpmp = False
    settings.torrent_seed = seed_after
    manager = DownloadManager(db, settings=settings, max_concurrent=1)
    try:
        job = manager.add_torrent(
            magnet_from_torrent(torrent) if magnet else str(torrent_path),
            dest_dir=tmp_path / "download",
            options={"peers": [f"127.0.0.1:{seeder.listen_port()}"]},
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            fresh = db.get_job(job.id)
            assert fresh is not None
            if fresh.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            time.sleep(0.001)
        assert fresh is not None
        assert fresh.status is JobStatus.COMPLETED, fresh.error
        # No grace period: completion hooks/auto-open must already see the bytes.
        output = Path(fresh.dest_dir) / fresh.filename
        assert hashlib.sha256(output.read_bytes()).digest() == expected
    finally:
        manager.shutdown()
        for handle in SESSION.session().get_torrents():
            SESSION.remove(handle)
        seeder.remove_torrent(seed_handle)

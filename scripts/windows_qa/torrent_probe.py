"""Real dynamically addressed seed/download pairs, with immediate disk checks."""

from __future__ import annotations

import argparse
import gc
import hashlib
import random
import time
from pathlib import Path

import libtorrent as lt
from common import ROOT, case, digest, save

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.engines.torrent import SESSION, create_torrent_file, magnet_from_torrent
from app.tests.network_ports import torrent_port


def transfer(index: int, *, magnet: bool, seed_after: bool, pause_resume: bool = False) -> dict:
    directory = ROOT / "torrents" / f"{index:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    seed_dir = directory / "seed"
    seed_dir.mkdir(exist_ok=True)
    data = random.Random(7000 + index).randbytes(2 * 1024 * 1024)
    expected = hashlib.sha256(data).hexdigest()
    original = seed_dir / f"payload-{index}.bin"
    original.write_bytes(data)
    torrent = create_torrent_file(original)
    torrent_path = directory / "payload.torrent"
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
    while time.monotonic() < deadline and (
        not seed_handle.status().is_seeding or not seeder.listen_port()
    ):
        time.sleep(0.05)
    assert seed_handle.status().is_seeding, "Seeder failed readiness"
    port = seeder.listen_port()
    assert port > 0, {
        "reason": "Seeder could not bind a listening socket",
        "alerts": [a.message() for a in seeder.pop_alerts()],
    }
    database = Database(directory / "test.db")
    settings = Settings(database)
    settings.torrent_port = torrent_port()
    settings.torrent_dht = settings.torrent_upnp = settings.torrent_natpmp = False
    settings.torrent_seed = seed_after
    if pause_resume:
        settings.speed_limit_kbps = 128
    manager = DownloadManager(database, settings=settings, max_concurrent=1)
    observed = []
    started = time.monotonic()
    resumed = False
    try:
        job = manager.add_torrent(
            magnet_from_torrent(torrent) if magnet else str(torrent_path),
            dest_dir=directory / "download",
            options={"peers": [f"127.0.0.1:{port}"]},
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            fresh = database.get_job(job.id)
            assert fresh is not None
            snapshot = {
                "seconds": round(time.monotonic() - started, 3),
                "status": fresh.status.value,
                "downloaded": fresh.downloaded,
            }
            if not observed or any(
                snapshot[k] != observed[-1][k] for k in ("status", "downloaded")
            ):
                observed.append(snapshot)
            if pause_resume and not resumed and 0 < fresh.downloaded < len(data):
                manager.pause(job.id)
                pause_deadline = time.monotonic() + 8
                while time.monotonic() < pause_deadline:
                    if database.get_job(job.id).status is JobStatus.PAUSED:
                        break
                    time.sleep(0.05)
                assert database.get_job(job.id).status is JobStatus.PAUSED
                manager.resume(job.id)
                resumed = True
            if fresh.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            time.sleep(0.05)
        fresh = database.get_job(job.id)
        trace = {
            "magnet": magnet,
            "seed_after": seed_after,
            "pause_resume": pause_resume,
            "seeder_port": port,
            "job": str(fresh),
            "observations": observed,
            "alerts": [a.message() for a in SESSION.session().pop_alerts()],
        }
        save(f"torrents/{index:04d}/trace.json", trace)
        assert fresh is not None and fresh.status is JobStatus.COMPLETED, trace
        output = Path(fresh.dest_dir) / fresh.filename
        actual = digest(output)
        trace["immediate_sha256"] = actual
        if actual != expected:
            time.sleep(0.1)
            trace["after_100ms_sha256"] = digest(output)
            time.sleep(1)
            trace["after_1s_sha256"] = digest(output)
        trace["expected_sha256"] = expected
        save(f"torrents/{index:04d}/trace.json", trace)
        assert actual == expected, trace
        assert not pause_resume or resumed, "Transfer finished before pause/resume was exercised"
        return {
            "bytes": len(data),
            "sha256": actual,
            "magnet": magnet,
            "seed_after": seed_after,
            "pause_resume": resumed,
            "seeder_port": port,
            "seconds": round(time.monotonic() - started, 3),
        }
    finally:
        manager.shutdown()
        for handle in SESSION.session().get_torrents():
            SESSION.remove(handle)
        seeder.remove_torrent(seed_handle)
        del seed_handle, seeder
        database.close()
        gc.collect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--pause-resume", action="store_true")
    args = parser.parse_args()
    for index in range(args.offset, args.offset + args.count):
        with case(f"TOR-TRANSFER-{index:04d}", "local integration") as evidence:
            evidence.update(
                transfer(
                    index,
                    magnet=bool(index % 2),
                    seed_after=bool(index % 4 // 2),
                    pause_resume=args.pause_resume,
                )
            )

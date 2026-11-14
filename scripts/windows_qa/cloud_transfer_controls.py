"""Real cloud transport controls triggered at a deterministic received-chunk boundary."""

from __future__ import annotations

import hashlib
import time

from common import case

from app.core.models import JobKind, JobStatus
from app.engines.cloud import CloudDownload


def controls(db, store, url, dest, data, scheme, insecure=False):
    class Controlled(CloudDownload):
        action = ""

        def _advance(self, n):
            if self.action and n:
                action = self.action
                self.action = ""
                self.pause() if action == "pause" else self.cancel()
            super()._advance(n)

    with case(
        "CLOUD-TRANSFER-" + scheme.upper() + "-PAUSE-RESUME-CANCEL",
        "real authenticated local transport; pause/cancel commands at received-chunk boundary",
    ) as e:
        tag = str(time.time_ns())
        job = db.create_job(url, str(dest), "pause-" + tag + ".bin", kind=JobKind.CLOUD)
        task = Controlled(db, job, credentials=store, insecure=insecure)
        task.action = "pause"
        assert task.run() is JobStatus.PAUSED, db.get_job(job.id).error
        assert job.part_path.exists() and not job.dest_path.exists()
        assert 0 < job.part_path.stat().st_size < len(data)
        partial = job.part_path.stat().st_size
        assert job.part_path.read_bytes() == data[:partial]
        assert (
            CloudDownload(db, db.get_job(job.id), credentials=store, insecure=insecure).run()
            is JobStatus.COMPLETED
        )
        assert job.dest_path.read_bytes() == data
        cancelled = db.create_job(url, str(dest), "cancel-" + tag + ".bin", kind=JobKind.CLOUD)
        task = Controlled(db, cancelled, credentials=store, insecure=insecure)
        task.action = "cancel"
        assert task.run() is JobStatus.CANCELLED, db.get_job(cancelled.id).error
        assert not cancelled.part_path.exists() and not cancelled.dest_path.exists()
        e.update(
            paused_prefix_bytes=partial,
            job_ids=[job.id, cancelled.id],
            sha256=hashlib.sha256(data).hexdigest(),
            cancel_removed_partial=True,
            trigger="Calls genuine task.pause()/cancel() at the first received block; "
            "network, authentication and files are real.",
        )

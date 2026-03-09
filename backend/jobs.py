"""
Lightweight in-memory job manager for long-running tasks.

Jobs track burst captures and image stacking so the frontend can:
  1. Submit work and get an immediate job ID back (HTTP 202).
  2. Poll ``GET /api/jobs/{id}`` for progress updates.
  3. See completed/failed jobs in ``GET /api/jobs``.

Design constraints (Raspberry Pi):
  - No external broker or database – jobs live in a plain dict.
  - At most one camera job at a time (burst/capture) since the USB
    interface is exclusive.  Stacking jobs run independently.
  - Thread-based, not async, to match the existing synchronous codebase.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How many completed/failed jobs to keep for the frontend to query.
_MAX_HISTORY = 50


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Job:
    id: str
    type: str  # "burst" or "stack"
    status: JobStatus = JobStatus.queued
    progress: int = 0
    total: int = 0
    message: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self):
        self._cancel.set()

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "status": self.status.value,
            "progress": self.progress,
            "total": self.total,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        return d


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, job_type: str, total: int = 0, message: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            type=job_type,
            total=total,
            message=message,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._trim_history()
        logger.info("Job %s created: type=%s total=%d", job.id, job_type, total)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    def start(self, job: Job):
        job.status = JobStatus.running
        job.started_at = time.time()

    def update_progress(self, job: Job, progress: int, message: str = ""):
        job.progress = progress
        if message:
            job.message = message

    def complete(self, job: Job, result: dict):
        job.status = JobStatus.completed
        job.progress = job.total
        job.result = result
        job.finished_at = time.time()
        logger.info("Job %s completed in %.1fs", job.id, job.finished_at - (job.started_at or job.created_at))

    def fail(self, job: Job, error: str):
        job.status = JobStatus.failed
        job.error = error
        job.finished_at = time.time()
        logger.error("Job %s failed: %s", job.id, error)

    def cancel(self, job: Job):
        job.request_cancel()
        job.status = JobStatus.cancelled
        job.finished_at = time.time()
        logger.info("Job %s cancelled", job.id)

    def _trim_history(self):
        """Remove oldest finished jobs if we exceed _MAX_HISTORY."""
        finished = [
            j for j in self._jobs.values()
            if j.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled)
        ]
        if len(finished) > _MAX_HISTORY:
            finished.sort(key=lambda j: j.finished_at or 0)
            for j in finished[: len(finished) - _MAX_HISTORY]:
                del self._jobs[j.id]


# Singleton instance used by the app.
jobs = JobManager()

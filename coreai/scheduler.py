"""
CoreAI Protocol Suite - Scheduler
Lightweight background job scheduler for periodic maintenance tasks.
Handles cache eviction, stats snapshots, health checks, and log rotation triggers.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Optional
from enum import Enum

from loguru import logger


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    name: str
    func: Callable[[], Awaitable[None]]
    interval_seconds: float
    last_run: Optional[datetime] = None
    next_run: datetime = field(default_factory=datetime.utcnow)
    run_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    enabled: bool = True

    def is_due(self) -> bool:
        return self.enabled and datetime.utcnow() >= self.next_run

    def schedule_next(self):
        self.next_run = datetime.utcnow() + timedelta(seconds=self.interval_seconds)


class Scheduler:
    """
    Async background job scheduler.
    Jobs run in the background on their configured intervals.
    """

    def __init__(self, tick_interval: float = 1.0):
        self._jobs: dict[str, Job] = {}
        self._tick_interval = tick_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._total_runs = 0
        self._total_errors = 0

    async def start(self):
        """Start the scheduler loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler-loop")
        logger.debug(f"Scheduler started ({len(self._jobs)} job(s) registered)")

    async def stop(self):
        """Stop the scheduler and cancel the loop task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.debug("Scheduler stopped")

    def register(
        self,
        name: str,
        func: Callable[[], Awaitable[None]],
        interval_seconds: float,
        run_immediately: bool = False,
    ):
        """Register a recurring background job."""
        next_run = (
            datetime.utcnow()
            if run_immediately
            else (datetime.utcnow() + timedelta(seconds=interval_seconds))
        )
        self._jobs[name] = Job(
            name=name,
            func=func,
            interval_seconds=interval_seconds,
            next_run=next_run,
        )
        logger.debug(f"Registered job '{name}' (every {interval_seconds}s)")

    def unregister(self, name: str):
        self._jobs.pop(name, None)

    def enable(self, name: str):
        if name in self._jobs:
            self._jobs[name].enabled = True

    def disable(self, name: str):
        if name in self._jobs:
            self._jobs[name].enabled = False

    async def run_now(self, name: str):
        """Manually trigger a job immediately."""
        job = self._jobs.get(name)
        if not job:
            raise ValueError(f"Job '{name}' not registered")
        await self._run_job(job)

    async def _loop(self):
        """Main scheduler tick loop."""
        while self._running:
            for job in list(self._jobs.values()):
                if job.is_due():
                    asyncio.create_task(self._run_job(job), name=f"job-{job.name}")
            await asyncio.sleep(self._tick_interval)

    async def _run_job(self, job: Job):
        """Execute a single job, catching and logging errors."""
        job.last_run = datetime.utcnow()
        job.schedule_next()

        try:
            await job.func()
            job.run_count += 1
            self._total_runs += 1
            logger.debug(f"Job '{job.name}' completed (run #{job.run_count})")
        except Exception as e:
            job.error_count += 1
            job.last_error = str(e)
            self._total_errors += 1
            logger.warning(f"Job '{job.name}' failed: {str(e)[:120]}")

    def stats(self) -> dict:
        return {
            "running": self._running,
            "total_runs": self._total_runs,
            "total_errors": self._total_errors,
            "jobs": {
                name: {
                    "enabled": job.enabled,
                    "interval_seconds": job.interval_seconds,
                    "run_count": job.run_count,
                    "error_count": job.error_count,
                    "last_run": job.last_run.isoformat() if job.last_run else None,
                    "next_run": job.next_run.isoformat(),
                    "last_error": job.last_error,
                }
                for name, job in self._jobs.items()
            },
        }

"""In-process polling scheduler."""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .monitor import MonitorService
from .timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)


class MonitorScheduler:
    def __init__(self, settings: Settings, service: MonitorService):
        self.settings = settings
        self.service = service
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        first_run = (
            utc_now() + timedelta(seconds=2)
            if self.settings.check_on_start
            else utc_now() + timedelta(minutes=self.settings.check_interval_minutes)
        )
        self.scheduler.add_job(
            self._run,
            trigger=IntervalTrigger(
                minutes=self.settings.check_interval_minutes,
                jitter=60,
            ),
            id="pdf-monitor",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            next_run_time=first_run,
        )
        self.scheduler.start()
        logger.info(
            "scheduler started interval_minutes=%s first_run=%s",
            self.settings.check_interval_minutes,
            to_iso(first_run),
        )

    async def _run(self) -> None:
        logger.info("scheduled check started")
        try:
            results = await self.service.check_all()
        except Exception:
            logger.exception("scheduled check crashed")
            return
        changed = sum(1 for result in results if result.get("changed"))
        errors = sum(1 for result in results if result.get("outcome") == "error")
        logger.info(
            "scheduled check finished documents=%s changed=%s errors=%s",
            len(results),
            changed,
            errors,
        )

    @property
    def running(self) -> bool:
        return bool(self.scheduler.running)

    @property
    def next_run_at(self) -> str | None:
        job = self.scheduler.get_job("pdf-monitor")
        if job is None or job.next_run_time is None:
            return None
        return to_iso(job.next_run_time)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")

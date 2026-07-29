"""Cron scheduling of server actions (M10).

One process-wide ``AsyncIOScheduler``; every **enabled** ``Schedule`` row maps
to one cron job (job id = row id, so DB and scheduler stay in step). The CRUD
endpoints mutate the row first, then call ``sync``/``remove`` here. Design
follows Crafty's cron-only subset (ref docs/references/crafty-4.md): no
interval triggers, no chained tasks; ``one_time`` rows delete themselves after
their first firing.

Firing dispatches through the process ``manager`` (start/stop/restart/send)
or ``create_backup`` (trigger="scheduled"). Failures never raise out of the
job — they are published to the server's console hub, so the console shows
*why* a scheduled action didn't happen next to the server's own log lines.

``next_run`` is computed from the cron trigger directly (not from live job
state) so it works the same under tests, before ``start()``, and for disabled
rows being previewed.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from .db import engine
from .models import Schedule, ScheduleAction

log = logging.getLogger(__name__)


def validate_cron(expr: str) -> None:
    """Raise ``ValueError`` unless ``expr`` is a valid 5-field crontab."""
    CronTrigger.from_crontab(expr)


def next_run_time(schedule: Schedule) -> datetime | None:
    """Next firing of a schedule's cron in local time; ``None`` when disabled
    or the expression is invalid/has no future occurrence."""
    if not schedule.enabled:
        return None
    try:
        trigger = CronTrigger.from_crontab(schedule.cron)
    except ValueError:
        return None
    return trigger.get_next_fire_time(None, datetime.now(trigger.timezone))


class SchedulerService:
    """Owns the AsyncIOScheduler and keeps its jobs mirroring Schedule rows."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Load every enabled schedule and start ticking (app lifespan)."""
        with Session(engine) as session:
            for row in session.exec(select(Schedule)).all():
                self.sync(row)
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # --- row ↔ job sync ------------------------------------------------------

    def sync(self, schedule: Schedule) -> None:
        """Make the scheduler reflect one row: enabled → (re)schedule its job,
        disabled → drop it. Invalid cron rows are skipped (the API validates
        writes; this guards rows edited out of band)."""
        self.remove(schedule.id)
        if not schedule.enabled:
            return
        try:
            trigger = CronTrigger.from_crontab(schedule.cron)
        except ValueError:
            log.warning("schedule %s has invalid cron %r — skipped", schedule.id, schedule.cron)
            return
        self._scheduler.add_job(
            self._fire, trigger, args=[schedule.id], id=schedule.id,
            replace_existing=True,
            # A missed run (backend was down / clock jump) fires once on
            # catch-up if within the hour; stacked misses collapse to one.
            misfire_grace_time=3600, coalesce=True,
        )

    def remove(self, schedule_id: str) -> None:
        if self._scheduler.get_job(schedule_id) is not None:
            self._scheduler.remove_job(schedule_id)

    # --- firing --------------------------------------------------------------

    async def _fire(self, schedule_id: str) -> None:
        """Execute one schedule. Never raises: failures land in the console
        hub. ``one_time`` rows are removed after firing, success or not —
        the moment has passed."""
        with Session(engine) as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None or not schedule.enabled:
                self.remove(schedule_id)  # stale job for a deleted/disabled row
                return
            server_id = schedule.server_id
            try:
                await self._dispatch(session, schedule)
            except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
                from . import events
                from .servers.manager import manager

                log.warning("schedule %s (%s) failed: %s", schedule_id, schedule.action, exc)
                events.record(server_id, "schedule_failed", f"{schedule.action}: {exc}")
                manager.hub.publish(
                    server_id,
                    f"[lectern] scheduled {schedule.action} failed: {exc}",
                )
            if schedule.one_time:
                self.remove(schedule_id)
                # Re-fetch: _dispatch may have expired the session's map.
                row = session.get(Schedule, schedule_id)
                if row is not None:
                    session.delete(row)
                    session.commit()

    async def _dispatch(self, session: Session, schedule: Schedule) -> None:
        from .servers.manager import manager

        action = schedule.action
        server_id = schedule.server_id
        if action == ScheduleAction.start.value:
            # Scheduled starts are intentional — grant a fresh set of
            # crash-restart attempts, same as a user-initiated start.
            manager.reset_crash_count(server_id)
            await manager.start(server_id)
        elif action == ScheduleAction.stop.value:
            await manager.stop(server_id)
        elif action == ScheduleAction.restart.value:
            await manager.restart(server_id)
        elif action == ScheduleAction.backup.value:
            from .backups import create_backup
            from .models import Server

            server = session.get(Server, server_id)
            if server is None:
                raise ValueError("Server not found")
            await create_backup(session, server, trigger="scheduled")
        elif action == ScheduleAction.command.value:
            if not (schedule.command or "").strip():
                raise ValueError("Schedule has no command to send")
            await manager.send(server_id, schedule.command)
        else:
            raise ValueError(f"Unknown schedule action: {action}")


# Singleton — mirrors the `manager` pattern; wired into the app lifespan.
scheduler_service: SchedulerService = SchedulerService()

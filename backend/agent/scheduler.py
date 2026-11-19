"""Autonomous monitoring loop for the Field Resilience Agent.

Every tick it (1) evaluates recommendations whose follow-up time has passed, which
triggers re-planning, and (2) runs a monitoring cycle for each field whose last
cycle is older than AGENT_MONITOR_INTERVAL_HOURS. Fields run sequentially.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agent import store
from agent.outcomes import evaluate_action
from agent.runner import run_and_wait
from agent.tools import field_report_service
from config.postgres import AsyncSessionLocal
from config.settings import settings
from services.operation_event_service import operation_event_log

logger = logging.getLogger(__name__)


class FieldAgentMonitorService:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if not settings.AGENT_MONITOR_ENABLED or (self._task and not self._task.done()):
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="field-agent-monitor")
        operation_event_log.record_job_event(
            source="field_resilience_agent",
            severity="info",
            state="scheduler_started",
            message="Field Resilience Agent monitor started.",
            details={
                "interval_hours": settings.AGENT_MONITOR_INTERVAL_HOURS,
                "tick_seconds": settings.AGENT_MONITOR_TICK_SECONDS,
            },
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "enabled": settings.AGENT_MONITOR_ENABLED,
            "running": bool(self._task and not self._task.done()),
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "interval_hours": settings.AGENT_MONITOR_INTERVAL_HOURS,
        }

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_loop(self) -> None:
        if await self._sleep(settings.AGENT_MONITOR_INITIAL_DELAY_SECONDS):
            return
        while not self._stop_event.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Field agent monitor tick failed")
            self.last_tick_at = datetime.now(timezone.utc)
            if await self._sleep(settings.AGENT_MONITOR_TICK_SECONDS):
                return

    async def tick(self) -> None:
        # Safety net: a run that outlived its timeout without finishing is dead; release it.
        released = await store.fail_stale_runs(store.RUN_TIMEOUT_MINUTES, "Run stopped responding and was released by the monitor.")
        if released:
            logger.warning("Released %s stale field agent run(s)", released)

        for action in await store.due_follow_ups():
            try:
                await evaluate_action(action["action_id"], trigger_follow_up=True)
            except Exception:
                logger.exception("Follow-up evaluation failed for %s", action["action_id"])

        async with AsyncSessionLocal() as session:
            fields = await field_report_service.list(session, limit=500)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.AGENT_MONITOR_INTERVAL_HOURS)
        for field in fields:
            if self._stop_event.is_set():
                return
            field_id = field["field_id"]
            field_settings = await store.get_settings(field_id)
            if not field_settings.get("monitor_enabled", True):
                continue
            last = await store.last_run_started_at(field_id)
            if last and last > cutoff:
                continue
            await run_and_wait(field_id, trigger="scheduled")

"""Outcome evaluation: measure what changed after a recommendation, then re-plan.

When an approved (or informational) action reaches its follow-up time, AgriLens
refreshes evidence, re-runs the analyses, compares them with the baseline captured
when the action was proposed, stores a verdict, and starts a follow-up agent cycle
that receives the measured outcome.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from agent import evidence as ev
from agent import store
from agent.tools import (
    cache_service,
    drought_service,
    irrigation_service,
    moisture_service,
    refresh_service,
)
from config.postgres import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _delta(after: Any, before: Any) -> Optional[float]:
    if isinstance(after, (int, float)) and isinstance(before, (int, float)):
        return round(after - before, 2)
    return None


def judge_outcome(action: dict[str, Any], baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    deltas = {
        "moisture_score": _delta(current.get("moisture_score"), baseline.get("moisture_score")),
        "drought_score": _delta(current.get("drought_score"), baseline.get("drought_score")),
        "root_zone_depletion_mm": _delta(current.get("root_zone_depletion_mm"), baseline.get("root_zone_depletion_mm")),
    }
    depletion = deltas["root_zone_depletion_mm"]
    moisture = deltas["moisture_score"]
    drought = deltas["drought_score"]
    improved = (depletion is not None and depletion <= -3) or (moisture is not None and moisture >= 5)
    worsened = (depletion is not None and depletion >= 5) or (drought is not None and drought >= 10) or (moisture is not None and moisture <= -10)

    action_type = action.get("action_type")
    if action_type in ("inspect_zone", "collect_sensor_data"):
        verdict = "field_report_received" if action.get("field_note") else "awaiting_field_report"
        message = (
            f"Farmer reported: {action['field_note']}" if action.get("field_note")
            else "No field report was recorded; the agent will ask again if the evidence conflict persists."
        )
    elif worsened:
        verdict = "worsened"
        message = "Conditions deteriorated after the recommendation; the plan needs revision."
    elif improved:
        verdict = "improved"
        message = "Root-zone water status improved after the recommendation."
    else:
        verdict = "stable"
        message = "Conditions stayed within the expected range."

    if action_type == "irrigate" and action.get("applied_mm") is not None:
        message += f" Farmer applied {action['applied_mm']} mm (recommended {action.get('water_mm')} mm)."
    if action_type == "wait_for_rain":
        message += f" Forecast at proposal time was {baseline.get('rain_next_48h_mm')} mm in 48 h."
    return {"verdict": verdict, "message": message, "deltas": deltas, "baseline": baseline, "current": current}


async def measure_current(field_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        try:
            await refresh_service.refresh_from_red_dots(session, field_id, event_source="field_agent_follow_up")
        except Exception:
            await session.rollback()
            logger.exception("Follow-up refresh failed for %s", field_id)
        moisture = await moisture_service.analyze(session, field_id)
        drought = await drought_service.analyze(session, field_id)
        irrigation = await irrigation_service.analyze(session, field_id)
        palette = await cache_service.weather_palette(session, field_id)
    evidence = {
        "moisture": ev.summarize_moisture(moisture.model_dump(mode="json")) if moisture else {},
        "drought": ev.summarize_drought(drought.model_dump(mode="json")) if drought else {},
        "irrigation": ev.summarize_irrigation(irrigation.model_dump(mode="json")) if irrigation else {},
        "weather": ev.summarize_weather(palette) if palette else {},
    }
    return ev.baseline_snapshot(evidence)


async def evaluate_action(action_id: str, trigger_follow_up: bool = True) -> Optional[dict[str, Any]]:
    action = await store.get_action(action_id)
    if not action or action["status"] not in store.FOLLOW_UP_STATUSES:
        return None
    current = await measure_current(action["field_id"])
    outcome = judge_outcome(action, action.get("baseline") or {}, current)
    await store.record_outcome(action_id, outcome)
    follow_up_run = None
    if trigger_follow_up:
        from agent.runner import start_run  # local import avoids a cycle

        follow_up_run = await start_run(
            action["field_id"],
            trigger="follow_up",
            context={
                "follow_up_of": action_id,
                "follow_up": {
                    "action": {k: action.get(k) for k in ("action_type", "title", "water_mm", "applied_mm", "decision_note", "field_note")},
                    "verdict": outcome["verdict"],
                    "message": outcome["message"],
                    "deltas": outcome["deltas"],
                },
            },
        )
    return {"action_id": action_id, "outcome": outcome, "follow_up_run": follow_up_run}

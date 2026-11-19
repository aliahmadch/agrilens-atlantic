"""Deterministic safety policy for agent proposals.

The language model plans; this module decides what is allowed. A proposal that
violates a blocking rule is returned to the agent with the reason so it can revise
its plan (for example, collect evidence first or wait for forecast rain). The policy
is the same for the LLM agent and the rule-based fallback planner.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from agent.evidence import all_zone_ids


ACTION_TYPES = ("irrigate", "wait_for_rain", "inspect_zone", "collect_sensor_data", "monitor")
APPROVAL_REQUIRED = {"irrigate", "inspect_zone", "collect_sensor_data"}
PRIORITIES = ("high", "medium", "low")
MAX_ACCEPTED_PER_RUN = 3
RAIN_COVERAGE_HOURS = 48
PLANNER_TOLERANCE = 1.10
MIN_FOLLOW_UP_HOURS = 6
MAX_FOLLOW_UP_HOURS = 168


def _check(rule: str, passed: bool, message: str, blocking: bool = True) -> dict[str, Any]:
    return {"rule": rule, "passed": passed, "severity": "block" if blocking else "warn", "message": message}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_proposal(raw: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Validate shape and coerce types. Returns (proposal, errors)."""
    errors: list[str] = []
    action_type = str(raw.get("action_type") or "").strip()
    if action_type not in ACTION_TYPES:
        errors.append(f"action_type must be one of {', '.join(ACTION_TYPES)}")
    title = str(raw.get("title") or "").strip()
    rationale = str(raw.get("rationale") or "").strip()
    if not title:
        errors.append("title is required")
    if not rationale:
        errors.append("rationale is required")
    confidence = _number(raw.get("confidence"))
    if confidence is None or not 0 <= confidence <= 1:
        errors.append("confidence must be a number between 0 and 1")
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty list of {source, observation}")
        evidence = []
    clean_evidence = []
    for item in evidence[:10]:
        if isinstance(item, dict):
            clean_evidence.append({"source": str(item.get("source") or "")[:80], "observation": str(item.get("observation") or "")[:400]})
        else:
            clean_evidence.append({"source": "agent", "observation": str(item)[:400]})
    assumptions = [str(item)[:300] for item in (raw.get("assumptions") or [])[:8]]
    priority = str(raw.get("priority") or "medium")
    if priority not in PRIORITIES:
        priority = "medium"
    follow_up = _number(raw.get("follow_up_hours"))
    follow_up_hours = int(min(MAX_FOLLOW_UP_HOURS, max(MIN_FOLLOW_UP_HOURS, follow_up if follow_up is not None else 24)))
    target_date: Optional[date] = None
    if raw.get("target_date"):
        try:
            target_date = date.fromisoformat(str(raw["target_date"])[:10])
        except ValueError:
            errors.append("target_date must be YYYY-MM-DD")
    zone_id = str(raw["zone_id"]).strip() if raw.get("zone_id") else None
    if errors:
        return None, errors
    return {
        "action_type": action_type,
        "title": title[:200],
        "rationale": rationale[:2000],
        "evidence": clean_evidence,
        "assumptions": assumptions,
        "confidence": round(confidence, 3),
        "priority": priority,
        "water_mm": _number(raw.get("water_mm")),
        "target_date": target_date,
        "zone_id": zone_id,
        "follow_up_hours": follow_up_hours,
    }, []


def evaluate_proposal(
    proposal: dict[str, Any],
    evidence: dict[str, Any],
    tools_called: set[str],
    settings: dict[str, Any],
    accepted_so_far: list[dict[str, Any]],
    recent_actions: list[dict[str, Any]],
    refresh_attempted: bool,
) -> dict[str, Any]:
    """Return {accepted, checks, proposal} where proposal is adjusted (volume, approval flag)."""
    checks: list[dict[str, Any]] = []
    action_type = proposal["action_type"]
    irrigation = evidence.get("irrigation") or {}
    weather = evidence.get("weather") or {}
    freshness = evidence.get("freshness") or {}
    profile = evidence.get("profile") or {}

    checks.append(_check(
        "proposal_budget",
        len(accepted_so_far) < MAX_ACCEPTED_PER_RUN,
        f"At most {MAX_ACCEPTED_PER_RUN} actions per monitoring cycle keeps recommendations focused.",
    ))
    duplicate = any(item["action_type"] == action_type and item.get("zone_id") == proposal.get("zone_id") for item in accepted_so_far)
    checks.append(_check("no_duplicate", not duplicate, "An equivalent action was already accepted in this cycle."))
    checks.append(_check(
        "evidence_freshness_checked",
        "check_evidence_freshness" in tools_called,
        "Evidence freshness must be verified before any recommendation.",
    ))

    if action_type == "irrigate":
        checks.append(_check(
            "irrigation_planner_consulted",
            "plan_irrigation" in tools_called,
            "Irrigation amounts must come from the FAO-56 root-zone water balance (plan_irrigation).",
        ))
        checks.append(_check(
            "forecast_consulted",
            "get_weather_outlook" in tools_called,
            "Forecast rainfall must be checked before applying water.",
        ))
        core_stale = freshness.get("verdict") == "stale_core_moisture"
        checks.append(_check(
            "fresh_moisture_evidence",
            not core_stale or refresh_attempted,
            "All core soil-moisture sources are stale; refresh sources (or collect sensor evidence) before irrigating.",
        ))
        min_conf = float(settings.get("min_confidence_for_irrigation") or 0.55)
        planner_conf = _number(irrigation.get("confidence")) or 0.0
        checks.append(_check(
            "confidence_threshold",
            proposal["confidence"] >= min_conf and planner_conf >= min_conf,
            f"Irrigation requires agent and planner confidence >= {min_conf:.2f} (agent {proposal['confidence']:.2f}, planner {planner_conf:.2f}). Below that, propose collect_sensor_data or inspect_zone.",
        ))
        water = proposal.get("water_mm")
        planner_gross = max(_number(irrigation.get("gross_irrigation_mm")) or 0.0, _number(irrigation.get("full_refill_gross_mm")) or 0.0)
        cap = float(settings.get("max_application_mm") or 35)
        checks.append(_check("positive_amount", water is not None and water > 0, "Irrigation needs a positive water_mm."))
        if water is not None and water > 0:
            checks.append(_check(
                "farmer_application_cap",
                water <= cap,
                f"Farmer limit is {cap:.0f} mm per application; proposed {water:.1f} mm.",
            ))
            checks.append(_check(
                "within_planner_requirement",
                planner_gross > 0 and water <= planner_gross * PLANNER_TOLERANCE,
                f"Planner gross requirement is {planner_gross:.1f} mm; the agent may not exceed it by more than 10% (proposed {water:.1f} mm)."
                if planner_gross > 0
                else "The irrigation planner currently computes no application requirement; propose monitor, wait_for_rain or inspect_zone instead.",
            ))
        rain_48h = _number(weather.get("rain_next_48h_mm"))
        net = _number(irrigation.get("net_irrigation_mm")) or 0.0
        if rain_48h is not None and net > 0:
            checks.append(_check(
                "forecast_rain_covers_need",
                rain_48h < net,
                f"Forecast rain in the next {RAIN_COVERAGE_HOURS} h ({rain_48h:.1f} mm) covers the net requirement ({net:.1f} mm); propose wait_for_rain to conserve water.",
            ))
    else:
        proposal["water_mm"] = None

    if action_type == "wait_for_rain":
        rain_7d = _number(weather.get("rain_next_7d_mm"))
        checks.append(_check(
            "forecast_supports_waiting",
            "get_weather_outlook" in tools_called and rain_7d is not None and rain_7d > 0,
            "Waiting for rain requires a forecast with measurable rainfall.",
        ))

    if action_type == "inspect_zone" and proposal.get("zone_id"):
        zones = all_zone_ids(evidence)
        checks.append(_check(
            "zone_exists",
            not zones or proposal["zone_id"] in zones,
            f"Zone {proposal['zone_id']} is not in the analysed zones ({', '.join(sorted(zones)) or 'none'}).",
        ))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recently_rejected = [
        item for item in recent_actions
        if item.get("action_type") == action_type and item.get("status") == "rejected"
        and item.get("decided_at") and item["decided_at"] >= cutoff
    ]
    if recently_rejected:
        note = recently_rejected[0].get("decision_note") or "no reason given"
        checks.append(_check(
            "respect_recent_rejection",
            True,
            f"The farmer rejected a similar {action_type} action in the last 48 h ({note}). Make the changed conditions explicit.",
            blocking=False,
        ))

    accepted = all(item["passed"] for item in checks if item["severity"] == "block")
    adjusted = dict(proposal)
    adjusted["requires_approval"] = action_type in APPROVAL_REQUIRED
    area = _number(profile.get("area_hectares"))
    adjusted["volume_m3"] = round(adjusted["water_mm"] * area * 10, 1) if adjusted.get("water_mm") and area else None
    adjusted["policy_checks"] = checks
    return {"accepted": accepted, "checks": checks, "proposal": adjusted}

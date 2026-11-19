"""Agent tools: thin wrappers over the existing AgriLens services.

Each tool calls a service the portal already uses, condenses its output with
agent.evidence, caches the result on the run context, and returns compact JSON.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from agent import evidence as ev
from agent import store
from agent.policy import ACTION_TYPES, evaluate_proposal, normalize_proposal
from services.drought_score_history_service import DroughtScoreHistoryService
from services.field_crop_health_stress_analysis_service import FieldCropHealthStressAnalysisService
from services.field_data_cache_service import FieldDataCacheService
from services.field_drought_analysis_service import FieldDroughtAnalysisService
from services.field_moisture_analysis_service import FieldMoistureAnalysisService
from services.field_report_service import FieldReportService
from services.field_sampling_service import FieldSamplingService
from services.irrigation_scheduling_service import IrrigationSchedulingService
from services.moisture_score_history_service import MoistureScoreHistoryService
from services.moisture_source_refresh_service import MoistureSourceRefreshService


field_report_service = FieldReportService()
field_sampling_service = FieldSamplingService()
cache_service = FieldDataCacheService()
refresh_service = MoistureSourceRefreshService()
moisture_service = FieldMoistureAnalysisService()
drought_service = FieldDroughtAnalysisService()
crop_service = FieldCropHealthStressAnalysisService()
irrigation_service = IrrigationSchedulingService()
moisture_history_service = MoistureScoreHistoryService()
drought_history_service = DroughtScoreHistoryService()

# Evidence derived from stored provider data; invalidated after a refresh.
ANALYSIS_KEYS = ("freshness", "moisture", "drought", "crop_health", "weather", "irrigation")

PHASE_BY_TOOL = {
    "get_field_profile": "observe",
    "review_past_actions": "observe",
    "check_evidence_freshness": "validate",
    "refresh_stale_sources": "validate",
    "assess_soil_moisture": "diagnose",
    "assess_drought_risk": "diagnose",
    "assess_crop_health": "diagnose",
    "get_recent_trends": "diagnose",
    "get_weather_outlook": "plan",
    "plan_irrigation": "plan",
    "propose_action": "propose",
    "finish_run": "propose",
}


class ToolError(Exception):
    pass


@dataclass
class AgentContext:
    field_id: str
    run_id: str
    session: AsyncSession
    settings: dict[str, Any]
    report: dict[str, Any]
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    tools_called: set[str] = field(default_factory=set)
    refresh_attempted: bool = False
    accepted_actions: list[dict[str, Any]] = field(default_factory=list)
    finish: Optional[dict[str, Any]] = None
    tool_call_count: int = 0


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def get_field_profile(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    if "profile" not in ctx.evidence:
        points = await field_sampling_service.list(ctx.session, ctx.field_id)
        ctx.evidence["profile"] = ev.summarize_profile(ctx.report, ctx.settings, len(points))
    return ctx.evidence["profile"]


async def review_past_actions(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    return ev.summarize_past_actions(ctx.recent_actions[:8])


async def check_evidence_freshness(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    if "freshness" not in ctx.evidence:
        cache = await cache_service.field_cache(ctx.session, ctx.field_id)
        ctx.evidence["freshness"] = ev.summarize_freshness(cache.get("statuses") or {})
    return ctx.evidence["freshness"]


async def refresh_stale_sources(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    if ctx.refresh_attempted:
        return {"skipped": True, "reason": "Sources were already refreshed in this cycle; use the analyses as they are."}
    ctx.refresh_attempted = True
    results = await refresh_service.refresh_from_red_dots(ctx.session, ctx.field_id, event_source="field_resilience_agent")
    for key in ANALYSIS_KEYS:
        ctx.evidence.pop(key, None)
    summary = ev.summarize_refresh(results)
    cache = await cache_service.field_cache(ctx.session, ctx.field_id)
    ctx.evidence["freshness"] = ev.summarize_freshness(cache.get("statuses") or {})
    summary["freshness_after_refresh"] = {
        "verdict": ctx.evidence["freshness"]["verdict"],
        "stale_sources": ctx.evidence["freshness"]["stale_sources"],
    }
    return summary


async def _analysis(ctx: AgentContext, key: str, service: Any, summarizer: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    if key not in ctx.evidence:
        result = await service.analyze(ctx.session, ctx.field_id)
        if result is None:
            raise ToolError("Field not found or analysis unavailable.")
        ctx.evidence[key] = summarizer(result.model_dump(mode="json"))
    return ctx.evidence[key]


async def assess_soil_moisture(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    return await _analysis(ctx, "moisture", moisture_service, ev.summarize_moisture)


async def assess_drought_risk(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    return await _analysis(ctx, "drought", drought_service, ev.summarize_drought)


async def assess_crop_health(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    return await _analysis(ctx, "crop_health", crop_service, ev.summarize_crop_health)


async def plan_irrigation(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    return await _analysis(ctx, "irrigation", irrigation_service, ev.summarize_irrigation)


async def get_weather_outlook(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    if "weather" not in ctx.evidence:
        palette = await cache_service.weather_palette(ctx.session, ctx.field_id)
        ctx.evidence["weather"] = ev.summarize_weather(palette)
    return ctx.evidence["weather"]


async def get_recent_trends(ctx: AgentContext, _: dict[str, Any]) -> dict[str, Any]:
    if "trends" not in ctx.evidence:
        moisture = await moisture_history_service.list_history(ctx.session, ctx.field_id, limit=24)
        drought = await drought_history_service.list_history(ctx.session, ctx.field_id, limit=10)
        ctx.evidence["trends"] = ev.summarize_trends(
            [item.model_dump(mode="json") for item in moisture.records],
            [item.model_dump(mode="json") for item in drought.records],
        )
    return ctx.evidence["trends"]


async def propose_action(ctx: AgentContext, raw: dict[str, Any]) -> dict[str, Any]:
    proposal, errors = normalize_proposal(raw)
    if errors or proposal is None:
        return {"accepted": False, "violations": errors, "guidance": "Fix the proposal fields and call propose_action again."}
    verdict = evaluate_proposal(
        proposal,
        ctx.evidence,
        ctx.tools_called,
        ctx.settings,
        ctx.accepted_actions,
        ctx.recent_actions,
        ctx.refresh_attempted,
    )
    if not verdict["accepted"]:
        return {
            "accepted": False,
            "violations": [item["message"] for item in verdict["checks"] if not item["passed"] and item["severity"] == "block"],
            "guidance": "Revise the plan to satisfy the safety policy (you may gather more evidence or choose a different action).",
        }
    action = verdict["proposal"]
    baseline = ev.baseline_snapshot(ctx.evidence)
    if action.get("zone_id"):
        baseline["zone"] = _zone_details(ctx.evidence, action["zone_id"])
    action["baseline"] = baseline
    saved = await store.create_action(ctx.run_id, ctx.field_id, action)
    ctx.accepted_actions.append(action)
    warnings = [item["message"] for item in verdict["checks"] if item["severity"] == "warn"]
    return {
        "accepted": True,
        "action_id": saved["action_id"],
        "status": saved["status"],
        "requires_farmer_approval": action["requires_approval"],
        "volume_m3": action.get("volume_m3"),
        "policy_warnings": warnings,
    }


async def finish_run(ctx: AgentContext, raw: dict[str, Any]) -> dict[str, Any]:
    risk = str(raw.get("risk_level") or "moderate")
    if risk not in ("low", "moderate", "high", "critical"):
        risk = "moderate"
    confidence = raw.get("confidence")
    ctx.finish = {
        "headline": str(raw.get("headline") or "Monitoring cycle complete.")[:240],
        "summary": str(raw.get("summary_for_farmer") or "")[:3000],
        "risk_level": risk,
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
    }
    return {"recorded": True, "accepted_actions": len(ctx.accepted_actions)}


def _zone_details(evidence: dict[str, Any], zone_id: str) -> dict[str, Any]:
    details: dict[str, Any] = {"zone_id": zone_id}
    for key in ("drought", "crop_health", "moisture"):
        for zone in (evidence.get(key) or {}).get("zones") or []:
            if zone.get("zone_id") == zone_id:
                details[key] = {k: v for k, v in zone.items() if k in ("risk_class", "health_class", "status", "score", "health_score", "confidence")}
                if zone.get("centroid") and "centroid" not in details:
                    details["centroid"] = zone["centroid"]
    return details


# ---------------------------------------------------------------------------
# Tool definitions exposed to the model
# ---------------------------------------------------------------------------

NO_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_field_profile",
        "description": "Field identity and agronomic setup: crop, growth stage, soil, irrigation method, area, the farmer's last observed condition, and farmer-defined constraints (max mm per application, minimum confidence for irrigation, notes).",
        "input_schema": NO_INPUT,
    },
    {
        "name": "review_past_actions",
        "description": "Previous agent recommendations for this field with farmer decisions, field notes, applied water and measured outcomes. Use it to avoid repeating rejected advice and to learn from outcomes.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "check_evidence_freshness",
        "description": "Age and provider status of every evidence source (Open-Meteo, ECCC weather; SMAP L4, ERA5-Land soil moisture; Sentinel-2 imagery). Reports stale/missing sources and whether providers have newer data. Must be called before proposing any action.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "refresh_stale_sources",
        "description": "Fetch newer provider data for stale or outdated sources and store it, then re-check freshness. Low-risk and reversible; allowed once per cycle. Analyses called afterwards use the refreshed data.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "assess_soil_moisture",
        "description": "Moisture Module: calibrated field and zone soil-moisture status (too_dry/low/adequate/wet/saturated_risk) from SMAP, ERA5-Land, Open-Meteo soil layers, water balance and Sentinel-2 moisture indices, with confidence, data gaps and source disagreement warnings.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "assess_drought_risk",
        "description": "Drought Module: field agricultural drought-risk score and class with FAO-56 root-zone deficit, water-balance deficit, heat stress and satellite stress components, per-zone risk and engine recommendation.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "assess_crop_health",
        "description": "Crop Health & Stress Module: Sentinel-2 vigor (NDVI/NDRE), canopy water (NDMI/MSI) and moisture context per zone, giving health class (healthy/watch/stressed/critical) and confidence.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "get_recent_trends",
        "description": "Recent moisture-score (hourly) and drought-score (daily) history with trend direction (improving/stable/worsening).",
        "input_schema": NO_INPUT,
    },
    {
        "name": "get_weather_outlook",
        "description": "7-day field forecast (combined Open-Meteo/ECCC): rain, rain probability, temperature and crop water demand per day, plus 48-hour and 7-day rain totals.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "plan_irrigation",
        "description": "Irrigation Scheduling engine (FAO-56 root-zone water balance): status (irrigate_now/irrigate_soon/plan_ahead/monitor/...), recommended date, net and gross irrigation mm, volume, current depletion vs readily available water, and a daily depletion projection that includes forecast rain. The only valid source for irrigation amounts.",
        "input_schema": NO_INPUT,
    },
    {
        "name": "propose_action",
        "description": (
            "Submit one recommended action for the farmer. A deterministic safety policy validates it and either accepts it "
            "(irrigate, inspect_zone and collect_sensor_data then wait for farmer approval; wait_for_rain and monitor are informational "
            "and are followed up automatically) or rejects it with the violated rules so you can revise. Submit 1-3 actions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": list(ACTION_TYPES)},
                "title": {"type": "string", "description": "Short imperative instruction for the farmer, e.g. 'Apply 18 mm by sprinkler on Nov 14'."},
                "rationale": {"type": "string", "description": "Why this action, in 2-4 plain sentences a farmer understands, citing the decisive evidence."},
                "evidence": {
                    "type": "array",
                    "description": "Specific observations from tool results that support the action.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Tool or data source, e.g. plan_irrigation, SMAP L4, Sentinel-2."},
                            "observation": {"type": "string", "description": "The concrete value or finding."},
                        },
                        "required": ["source", "observation"],
                        "additionalProperties": False,
                    },
                },
                "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Assumptions that would change the advice if wrong."},
                "confidence": {"type": "number", "description": "0-1 confidence in this action given evidence quality and agreement."},
                "water_mm": {"type": ["number", "null"], "description": "Gross irrigation depth in mm (irrigate only; must come from plan_irrigation)."},
                "target_date": {"type": ["string", "null"], "description": "YYYY-MM-DD when the action should happen."},
                "zone_id": {"type": ["string", "null"], "description": "Zone id from an assessment, for zone-specific actions."},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "follow_up_hours": {"type": "integer", "description": "When to re-evaluate the outcome (6-168 hours)."},
            },
            "required": ["action_type", "title", "rationale", "evidence", "confidence", "priority", "follow_up_hours"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish_run",
        "description": "Close the monitoring cycle with a farmer-facing summary. Call exactly once, after your proposals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {"type": "string", "description": "One sentence: the field's situation and what to do."},
                "risk_level": {"type": "string", "enum": ["low", "moderate", "high", "critical"]},
                "confidence": {"type": "number", "description": "0-1 overall confidence in the assessment."},
                "summary_for_farmer": {"type": "string", "description": "3-6 short sentences: what was checked, what was found (including evidence conflicts or staleness), what is recommended and what will be re-checked."},
            },
            "required": ["headline", "risk_level", "confidence", "summary_for_farmer"],
            "additionalProperties": False,
        },
    },
]

HANDLERS: dict[str, Callable[[AgentContext, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "get_field_profile": get_field_profile,
    "review_past_actions": review_past_actions,
    "check_evidence_freshness": check_evidence_freshness,
    "refresh_stale_sources": refresh_stale_sources,
    "assess_soil_moisture": assess_soil_moisture,
    "assess_drought_risk": assess_drought_risk,
    "assess_crop_health": assess_crop_health,
    "get_recent_trends": get_recent_trends,
    "get_weather_outlook": get_weather_outlook,
    "plan_irrigation": plan_irrigation,
    "propose_action": propose_action,
    "finish_run": finish_run,
}


async def execute_tool(ctx: AgentContext, name: str, tool_input: dict[str, Any]) -> tuple[dict[str, Any], bool, int]:
    """Run a tool. Returns (result, is_error, duration_ms)."""
    started = time.perf_counter()
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool {name}"}, True, 0
    ctx.tool_call_count += 1
    try:
        result = await handler(ctx, tool_input or {})
        ctx.tools_called.add(name)
        return result, False, int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # Tool failures are reported to the agent, not raised.
        try:
            await ctx.session.rollback()
        except Exception:
            pass
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}, True, int((time.perf_counter() - started) * 1000)

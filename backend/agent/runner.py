"""Field Resilience Agent runner.

`start_run` creates the run record and executes the cycle in the background so the
portal can poll the live trace. The cycle uses Claude with tool use when an API key
is configured, and a deterministic rule-based planner (same tools, same safety
policy) when it is not, or if the model becomes unavailable mid-run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import anthropic

from agent import alerts, gemini, groq, store
from agent.prompts import SYSTEM_PROMPT, build_user_message
from agent.tools import PHASE_BY_TOOL, TOOL_DEFINITIONS, AgentContext, execute_tool, field_report_service
from config.postgres import AsyncSessionLocal
from config.settings import settings
from services.operation_event_service import operation_event_log

logger = logging.getLogger(__name__)

GOAL = "Protect crop health while minimizing unnecessary water use, within agronomic safety rules and farmer constraints."
MAX_MODEL_TURNS = 14
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_background_tasks: set[asyncio.Task] = set()


def claude_available() -> bool:
    return bool(settings.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


PROVIDER_CONFIGURED = {"groq": lambda: groq.configured(), "gemini": lambda: gemini.configured(), "claude": claude_available}


def llm_providers(purpose: str = "chat") -> list[str]:
    """Configured AI providers in preference order for chat (AI_PROVIDER_ORDER) or monitoring cycles
    (AGENT_PROVIDER_ORDER). Empty means rules only."""
    raw = settings.AGENT_PROVIDER_ORDER if purpose == "agent" else settings.AI_PROVIDER_ORDER
    order = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return [name for name in order if name in PROVIDER_CONFIGURED and PROVIDER_CONFIGURED[name]()]


def llm_provider(purpose: str = "chat") -> Optional[str]:
    providers = llm_providers(purpose)
    return providers[0] if providers else None


def llm_available() -> bool:
    return llm_provider() is not None


def llm_model_name(provider: Optional[str] = None, purpose: str = "chat") -> Optional[str]:
    provider = provider or llm_provider(purpose)
    if provider == "claude":
        return settings.AGENT_MODEL
    if provider == "gemini":
        return gemini.current_model()
    if provider == "groq":
        return groq.current_model()
    return None


def _client() -> anthropic.AsyncAnthropic:
    if settings.ANTHROPIC_API_KEY:
        return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return anthropic.AsyncAnthropic()


class Recorder:
    """Writes the run trace step by step."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.seq = 0

    async def step(self, phase: str, kind: str, title: str, content: Optional[dict[str, Any]] = None, tool_name: Optional[str] = None, duration_ms: Optional[int] = None) -> None:
        self.seq += 1
        try:
            await store.add_step(self.run_id, self.seq, phase, kind, title, content or {}, tool_name, duration_ms)
        except Exception:
            logger.exception("Failed to record agent step")


def result_headline(name: str, result: dict[str, Any], is_error: bool) -> str:
    if is_error:
        return f"{name} failed: {result.get('error', 'unknown error')}"
    try:
        if name == "get_field_profile":
            return f"{result.get('farm_name')}: {result.get('crop_type') or 'crop not set'} ({result.get('crop_stage') or 'stage n/a'}) on {result.get('soil_type') or 'unknown soil'}, {result.get('area_hectares')} ha; farmer reports '{result.get('farmer_observed_condition') or 'no observation'}'"
        if name == "review_past_actions":
            return f"{len(result.get('actions_newest_first') or [])} previous recommendation(s) reviewed"
        if name == "check_evidence_freshness":
            stale = result.get("stale_sources") or []
            newer = result.get("sources_with_newer_provider_data") or []
            return f"Evidence {result.get('verdict', '').replace('_', ' ')}; stale: {', '.join(stale) or 'none'}; newer provider data: {', '.join(newer) or 'none'}"
        if name == "refresh_stale_sources":
            if result.get("skipped"):
                return "Refresh skipped (already refreshed this cycle)"
            failed = [item["source"] for item in result.get("failed") or []]
            return f"Refreshed {', '.join(result.get('refreshed_sources') or []) or 'nothing'}" + (f"; failed: {', '.join(failed)}" if failed else "")
        if name == "assess_soil_moisture":
            return f"Moisture {result.get('field_status')} ({result.get('field_score_0_100')}/100, confidence {result.get('confidence')}); {len(result.get('data_gaps') or [])} data gap(s)"
        if name == "assess_drought_risk":
            return f"Drought risk {str(result.get('risk_class')).replace('_', ' ')} ({result.get('drought_risk_score_0_100')}/100, confidence {result.get('confidence')})"
        if name == "assess_crop_health":
            return f"Crop health {result.get('health_class')} ({result.get('health_score_0_100')}/100, confidence {result.get('confidence')}) across {len(result.get('zones') or [])} zone(s)"
        if name == "get_recent_trends":
            return f"Moisture trend {result.get('moisture_trend')}; drought trend {result.get('drought_trend')}"
        if name == "get_weather_outlook":
            return f"Rain next 48 h {result.get('rain_next_48h_mm')} mm, next 7 d {result.get('rain_next_7d_mm')} mm vs crop demand {result.get('crop_demand_next_7d_mm')} mm"
        if name == "plan_irrigation":
            root = result.get("root_zone") or {}
            return f"Planner: {str(result.get('status')).replace('_', ' ')}; depletion {root.get('depletion_mm')} of {root.get('readily_available_water_mm')} mm trigger; gross {result.get('gross_irrigation_mm')} mm"
        if name == "propose_action":
            if result.get("accepted"):
                return "Policy accepted proposal" + (" (awaiting farmer approval)" if result.get("requires_farmer_approval") else " (informational, auto follow-up)")
            return "Policy rejected proposal: " + "; ".join(result.get("violations") or [])
        if name == "finish_run":
            return "Monitoring cycle closed"
    except Exception:
        pass
    return f"{name} completed"


async def call_tool(ctx: AgentContext, recorder: Recorder, name: str, tool_input: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    phase = PHASE_BY_TOOL.get(name, "observe")
    if name == "propose_action":
        title = f"Propose {tool_input.get('action_type', 'action')}: {tool_input.get('title', '')}"
    elif name == "finish_run":
        title = "Finish cycle"
    else:
        title = f"Call {name}"
    await recorder.step(phase, "tool_call", title, {"input": tool_input}, tool_name=name)
    result, is_error, duration_ms = await execute_tool(ctx, name, tool_input)
    kind = "policy" if name == "propose_action" else "tool_result"
    await recorder.step(phase, kind, result_headline(name, result, is_error), {"result": result, "is_error": is_error}, tool_name=name, duration_ms=duration_ms)
    return result, is_error


# ---------------------------------------------------------------------------
# LLM agent loop
# ---------------------------------------------------------------------------

async def run_llm_cycle(ctx: AgentContext, recorder: Recorder, trigger: str, context: dict[str, Any], usage: dict[str, int]) -> str:
    """Returns 'finished', 'ended' (no finish_run) or 'unavailable' (caller should fall back)."""
    client = _client()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_user_message(trigger, ctx.field_id, datetime.now(timezone.utc).isoformat(timespec="minutes"), context)}
    ]
    current_phase = "observe"
    for _ in range(MAX_MODEL_TURNS):
        try:
            response = await client.beta.messages.create(
                model=settings.AGENT_MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": settings.AGENT_EFFORT},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            await recorder.step("observe", "error", "Claude API key rejected; switching to deterministic planner", {})
            return "unavailable"
        except anthropic.RateLimitError:
            await recorder.step("observe", "error", "Claude API rate limited; switching to deterministic planner", {})
            return "unavailable"
        except anthropic.APIStatusError as exc:
            await recorder.step("observe", "error", f"Claude API error {exc.status_code}; switching to deterministic planner", {"message": str(exc)[:500]})
            return "unavailable"
        except anthropic.APIConnectionError:
            await recorder.step("observe", "error", "Claude API unreachable; switching to deterministic planner", {})
            return "unavailable"

        usage["input_tokens"] += (response.usage.input_tokens or 0) + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        usage["output_tokens"] += response.usage.output_tokens or 0

        if response.stop_reason == "refusal":
            await recorder.step("observe", "error", "Model declined this request; switching to deterministic planner", {})
            return "unavailable"

        for block in response.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                await recorder.step(current_phase, "reasoning", block.thinking.strip().split("\n")[0][:200], {"text": block.thinking})
            elif block.type == "text" and block.text.strip():
                await recorder.step(current_phase, "message", block.text.strip().split("\n")[0][:200], {"text": block.text})
            elif block.type == "tool_use":
                current_phase = PHASE_BY_TOOL.get(block.name, current_phase)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            # end_turn / max_tokens / stop_sequence: the model has stopped working.
            if ctx.finish is None:
                final_text = "\n".join(block.text for block in response.content if block.type == "text").strip()
                if final_text:
                    ctx.finish = {"headline": final_text.split("\n")[0][:240], "summary": final_text, "risk_level": None, "confidence": None}
            return "finished" if ctx.finish else "ended"

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if ctx.tool_call_count >= settings.AGENT_MAX_TOOL_CALLS and block.name not in ("propose_action", "finish_run"):
                result, is_error = {"error": "Tool budget for this cycle is exhausted. Propose actions with the evidence you have, then call finish_run."}, True
            else:
                result, is_error = await call_tool(ctx, recorder, block.name, block.input if isinstance(block.input, dict) else {})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": is_error,
            })
        if ctx.finish is not None:
            return "finished"
        messages.append({"role": "user", "content": tool_results})

    await recorder.step("propose", "error", "Model turn limit reached; completing with deterministic planner", {})
    return "ended"


async def run_gemini_cycle(ctx: AgentContext, recorder: Recorder, trigger: str, context: dict[str, Any], usage: dict[str, int]) -> str:
    """Same loop, tools and safety policy as run_llm_cycle, driven by Gemini. Same return values."""
    contents: list[dict[str, Any]] = [
        {"role": "user", "parts": [{"text": build_user_message(trigger, ctx.field_id, datetime.now(timezone.utc).isoformat(timespec="minutes"), context)}]}
    ]
    current_phase = "observe"
    for _ in range(MAX_MODEL_TURNS):
        try:
            result = await gemini.generate(contents, SYSTEM_PROMPT, TOOL_DEFINITIONS, max_output_tokens=8192)
        except gemini.GeminiError as exc:
            await recorder.step("observe", "error", f"Gemini unavailable ({str(exc)[:120]}); switching to deterministic planner", {"message": str(exc)})
            return "unavailable"
        candidate = result["candidate"]
        usage["input_tokens"] += int(result["usage"].get("promptTokenCount") or 0)
        usage["output_tokens"] += int(result["usage"].get("candidatesTokenCount") or 0) + int(result["usage"].get("thoughtsTokenCount") or 0)
        if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            await recorder.step("observe", "error", "Model declined this request; switching to deterministic planner", {})
            return "unavailable"

        text = gemini.text_of(candidate)
        calls = gemini.function_calls(candidate)
        if text:
            await recorder.step(current_phase, "message", text.split("\n")[0][:200], {"text": text})
        contents.append(gemini.model_turn(candidate))

        if not calls:
            if ctx.finish is None and text:
                ctx.finish = {"headline": text.split("\n")[0][:240], "summary": text, "risk_level": None, "confidence": None}
            return "finished" if ctx.finish else "ended"

        responses = []
        for call in calls:
            name = call.get("name") or ""
            current_phase = PHASE_BY_TOOL.get(name, current_phase)
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if ctx.tool_call_count >= settings.AGENT_MAX_TOOL_CALLS and name not in ("propose_action", "finish_run"):
                output, is_error = {"error": "Tool budget for this cycle is exhausted. Propose actions with the evidence you have, then call finish_run."}, True
            else:
                output, is_error = await call_tool(ctx, recorder, name, args)
            # Round-trip through JSON so dates and decimals become plain values.
            responses.append((call, json.loads(json.dumps({"is_error": is_error, "result": output}, default=str))))
        if ctx.finish is not None:
            return "finished"
        contents.append(gemini.function_response_turn(responses))

    await recorder.step("propose", "error", "Model turn limit reached; completing with deterministic planner", {})
    return "ended"


async def run_groq_cycle(ctx: AgentContext, recorder: Recorder, trigger: str, context: dict[str, Any], usage: dict[str, int]) -> str:
    """Same loop, tools and safety policy as run_llm_cycle, driven by Groq. Same return values."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(trigger, ctx.field_id, datetime.now(timezone.utc).isoformat(timespec="minutes"), context)},
    ]
    current_phase = "observe"
    for _ in range(MAX_MODEL_TURNS):
        try:
            result = await groq.generate(messages, TOOL_DEFINITIONS, max_tokens=8192, timeout=90)
        except groq.GroqError as exc:
            await recorder.step("observe", "error", f"Groq unavailable ({str(exc)[:120]})", {"message": str(exc)})
            return "unavailable"
        message = result["message"]
        usage["input_tokens"] += int(result["usage"].get("prompt_tokens") or 0)
        usage["output_tokens"] += int(result["usage"].get("completion_tokens") or 0)

        text = (message.get("content") or "").strip()
        calls = groq.tool_calls(message)
        if text:
            await recorder.step(current_phase, "message", text.split("\n")[0][:200], {"text": text})
        messages.append(groq.assistant_turn(message))

        if not calls:
            if ctx.finish is None and text:
                ctx.finish = {"headline": text.split("\n")[0][:240], "summary": text, "risk_level": None, "confidence": None}
            return "finished" if ctx.finish else "ended"

        for call_id, name, args in calls:
            current_phase = PHASE_BY_TOOL.get(name, current_phase)
            if ctx.tool_call_count >= settings.AGENT_MAX_TOOL_CALLS and name not in ("propose_action", "finish_run"):
                output, is_error = {"error": "Tool budget for this cycle is exhausted. Propose actions with the evidence you have, then call finish_run."}, True
            else:
                output, is_error = await call_tool(ctx, recorder, name, args)
            messages.append(groq.tool_result_turn(call_id, {"is_error": is_error, "result": output}))
        if ctx.finish is not None:
            return "finished"

    await recorder.step("propose", "error", "Model turn limit reached; completing with deterministic planner", {})
    return "ended"


CYCLES = {"groq": run_groq_cycle, "gemini": run_gemini_cycle, "claude": run_llm_cycle}


# ---------------------------------------------------------------------------
# Deterministic fallback planner
# ---------------------------------------------------------------------------

STRESS_WORDS = ("wilt", "stress", "dry", "yellow", "curl", "brown", "scorch")
WET_STATUSES = ("adequate", "wet", "saturated_risk")
DROUGHT_ORDER = ["no_risk", "watch", "moderate_field_risk", "high_field_risk", "severe_field_risk", "extreme_field_risk"]
RULES_DIAGNOSIS_TOOLS = (
    ("assess_soil_moisture", "moisture"),
    ("assess_drought_risk", "drought"),
    ("assess_crop_health", "crop_health"),
    ("get_recent_trends", "trends"),
    ("get_weather_outlook", "weather"),
    ("plan_irrigation", "irrigation"),
)


def _worst_zone(evidence: dict[str, Any]) -> Optional[dict[str, Any]]:
    crop_zones = [z for z in (evidence.get("crop_health") or {}).get("zones") or [] if isinstance(z.get("health_score"), (int, float))]
    if crop_zones:
        zone = min(crop_zones, key=lambda z: z["health_score"])
        return {"zone_id": zone["zone_id"], "reason": f"lowest crop health ({zone['health_score']}/100, {zone.get('health_class')})"}
    drought_zones = [z for z in (evidence.get("drought") or {}).get("zones") or [] if isinstance(z.get("score"), (int, float))]
    if drought_zones:
        zone = max(drought_zones, key=lambda z: z["score"])
        return {"zone_id": zone["zone_id"], "reason": f"highest drought risk ({zone['score']}/100)"}
    return None


async def run_rules_cycle(ctx: AgentContext, recorder: Recorder) -> None:
    await recorder.step("observe", "reasoning", "Deterministic planner: running the standard evidence sequence", {"text": "Rule-based planner using the same tools and safety policy as the LLM agent."})
    for name in ("get_field_profile", "review_past_actions", "check_evidence_freshness"):
        if name not in ctx.tools_called:
            await call_tool(ctx, recorder, name, {})
    freshness = ctx.evidence.get("freshness") or {}
    if not ctx.refresh_attempted and (freshness.get("verdict") != "fresh" or freshness.get("sources_with_newer_provider_data")):
        await recorder.step("validate", "reasoning", "Evidence is stale or providers have newer data; refreshing before diagnosis", {})
        await call_tool(ctx, recorder, "refresh_stale_sources", {})
    for name, key in RULES_DIAGNOSIS_TOOLS:
        if key not in ctx.evidence:
            await call_tool(ctx, recorder, name, {})

    if ctx.accepted_actions:
        await _rules_finish(ctx, recorder)
        return

    ev = ctx.evidence
    profile = ev.get("profile") or {}
    moisture = ev.get("moisture") or {}
    drought = ev.get("drought") or {}
    irrigation = ev.get("irrigation") or {}
    weather = ev.get("weather") or {}
    freshness = ev.get("freshness") or {}
    min_conf = float(ctx.settings.get("min_confidence_for_irrigation") or 0.55)
    cap = float(ctx.settings.get("max_application_mm") or 35)
    follow_hours = 24

    observed = str(profile.get("farmer_observed_condition") or "").lower()
    farmer_sees_stress = any(word in observed for word in STRESS_WORDS)
    conflict_gaps = [gap for gap in moisture.get("data_gaps") or [] if "conflict" in gap.lower() or "disagree" in gap.lower()]
    moisture_conf = moisture.get("confidence") or 0
    planner_conf = irrigation.get("confidence") or 0
    gross = irrigation.get("gross_irrigation_mm") or 0
    net = irrigation.get("net_irrigation_mm") or 0
    rain_48h = weather.get("rain_next_48h_mm") or 0
    rain_7d = weather.get("rain_next_7d_mm") or 0
    root = irrigation.get("root_zone") or {}
    proposals: list[dict[str, Any]] = []
    report_cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    recent_field_reports = [
        item for item in ctx.recent_actions
        if item.get("field_note") and item.get("completed_at") and item["completed_at"] >= report_cutoff
    ]

    base_evidence = [
        {"source": "plan_irrigation", "observation": f"Status {irrigation.get('status')}; depletion {root.get('depletion_mm')} mm vs trigger {root.get('readily_available_water_mm')} mm; gross need {gross} mm"},
        {"source": "get_weather_outlook", "observation": f"Rain next 48 h {rain_48h} mm; next 7 d {rain_7d} mm"},
        {"source": "assess_soil_moisture", "observation": f"Moisture {moisture.get('field_status')} {moisture.get('field_score_0_100')}/100 (confidence {moisture_conf})"},
    ]

    if irrigation.get("status") in ("irrigate_now", "irrigate_soon", "plan_ahead") and gross > 0:
        if rain_48h >= net > 0:
            proposals.append({
                "action_type": "wait_for_rain", "priority": "medium", "follow_up_hours": 48,
                "title": f"Hold irrigation: {rain_48h} mm rain forecast in 48 h",
                "rationale": f"The root-zone balance needs {net} mm net, and forecast rain over the next 48 hours ({rain_48h} mm) should cover it. Waiting conserves water; AgriLens will re-check after the rain window.",
                "evidence": base_evidence, "assumptions": ["Forecast rainfall materializes at the field"], "confidence": round(min(moisture_conf, planner_conf), 2),
            })
        elif moisture_conf >= min_conf and planner_conf >= min_conf:
            amount = round(min(gross, cap), 1)
            proposals.append({
                "action_type": "irrigate", "priority": "high" if irrigation.get("status") == "irrigate_now" else "medium", "follow_up_hours": 24,
                "title": f"Apply {amount} mm by {profile.get('irrigation_method') or 'irrigation'}" + (f" on {irrigation.get('recommended_date')}" if irrigation.get("recommended_date") else ""),
                "rationale": f"Projected root-zone depletion reaches the crop stress trigger and forecast rain does not cover the {net} mm net requirement." + (f" The planner's {gross} mm exceeds your {cap:.0f} mm cap, so apply {amount} mm now and re-check." if gross > cap else ""),
                "evidence": base_evidence, "assumptions": ["No unrecorded irrigation since the last moisture observation", f"Application efficiency {root.get('irrigation_efficiency')}"],
                "confidence": round(min(moisture_conf, planner_conf), 2), "water_mm": amount, "target_date": irrigation.get("recommended_date"),
            })
        else:
            proposals.append({
                "action_type": "collect_sensor_data", "priority": "high", "follow_up_hours": 24,
                "title": "Take a soil-moisture reading before irrigating",
                "rationale": f"The planner indicates a possible water need, but evidence confidence (moisture {moisture_conf}, planner {planner_conf}) is below your {min_conf} threshold. A probe or hand-feel reading will confirm whether to irrigate.",
                "evidence": base_evidence, "assumptions": [], "confidence": round(max(moisture_conf, planner_conf), 2),
            })

    if recent_field_reports:
        await recorder.step(
            "diagnose", "reasoning",
            "A recent farmer field report already addresses the evidence conflict; not re-sending an inspection",
            {"field_note": recent_field_reports[0]["field_note"]},
        )
    elif conflict_gaps or (farmer_sees_stress and moisture.get("field_status") in WET_STATUSES):
        zone = _worst_zone(ev)
        reason = []
        if conflict_gaps:
            reason.append("gridded soil moisture and Sentinel-2 moisture indices disagree")
        if farmer_sees_stress:
            reason.append(f"you reported '{profile.get('farmer_observed_condition')}' while soil-moisture models read {moisture.get('field_status')}")
        inspect_evidence = [{"source": "assess_soil_moisture", "observation": gap} for gap in conflict_gaps[:2]]
        if farmer_sees_stress:
            inspect_evidence.append({"source": "farmer report", "observation": f"Observed condition: {profile.get('farmer_observed_condition')}"})
        if zone:
            inspect_evidence.append({"source": "assess_crop_health", "observation": zone["reason"]})
        proposals.append({
            "action_type": "inspect_zone", "priority": "high" if farmer_sees_stress else "medium", "follow_up_hours": 24,
            "zone_id": zone["zone_id"] if zone else None,
            "title": f"Inspect {zone['zone_id'].replace('_', ' ') if zone else 'the field'} to resolve conflicting evidence",
            "rationale": "Evidence conflicts: " + " and ".join(reason) + ". " + (f"{zone['zone_id'].replace('_', ' ').capitalize()} has the {zone['reason']}. " if zone else "") + "A ground check (leaf turgor, soil feel at 15-30 cm, pests or disease) decides whether this is water stress.",
            "evidence": inspect_evidence,
            "assumptions": ["Stress symptoms may be non-water causes (nutrients, disease, frost)"],
            "confidence": 0.6,
        })

    worst_drought = drought.get("risk_class")
    if worst_drought in DROUGHT_ORDER and DROUGHT_ORDER.index(worst_drought) >= 3 and not any(p["action_type"] == "inspect_zone" for p in proposals):
        zone = _worst_zone(ev)
        proposals.append({
            "action_type": "inspect_zone", "priority": "high", "follow_up_hours": 24, "zone_id": zone["zone_id"] if zone else None,
            "title": "Inspect the highest-risk zone for drought stress",
            "rationale": f"Field drought risk is {worst_drought.replace('_', ' ')}. Verify crop condition in the most affected zone.",
            "evidence": [{"source": "assess_drought_risk", "observation": drought.get("summary") or ""}], "assumptions": [], "confidence": drought.get("confidence") or 0.5,
        })

    if (moisture_conf < 0.5 or freshness.get("verdict") == "stale_core_moisture") and not any(p["action_type"] == "collect_sensor_data" for p in proposals):
        proposals.append({
            "action_type": "collect_sensor_data", "priority": "medium", "follow_up_hours": 48,
            "title": "Add a ground soil-moisture reading",
            "rationale": "Satellite and model soil-moisture evidence is weak or out of date for this field. One probe reading per zone would anchor the water balance.",
            "evidence": [{"source": "check_evidence_freshness", "observation": f"Verdict {freshness.get('verdict')}; stale {', '.join(freshness.get('stale_sources') or []) or 'none'}"}],
            "assumptions": [], "confidence": 0.55,
        })

    if not proposals:
        rationale = f"Root-zone depletion ({root.get('depletion_mm')} mm) is well within readily available water ({root.get('readily_available_water_mm')} mm) and the forecast adds {rain_7d} mm of rain this week. AgriLens will re-check automatically."
        monitor_evidence = list(base_evidence)
        if recent_field_reports:
            note = recent_field_reports[0]["field_note"]
            rationale = f"Your field report ('{note}') is consistent with adequate soil water, so no water action is needed. " + rationale
            monitor_evidence.append({"source": "farmer field report", "observation": note})
        proposals.append({
            "action_type": "monitor", "priority": "low", "follow_up_hours": follow_hours,
            "title": "No irrigation needed; keep monitoring",
            "rationale": rationale,
            "evidence": monitor_evidence, "assumptions": [], "confidence": round(min(moisture_conf or 0.6, planner_conf or 0.6), 2),
        })

    for proposal in proposals[:3]:
        result, _ = await call_tool(ctx, recorder, "propose_action", proposal)
        if not result.get("accepted") and proposal["action_type"] == "irrigate":
            await recorder.step("propose", "reasoning", "Irrigation blocked by policy; requesting ground evidence instead", {"violations": result.get("violations")})
            await call_tool(ctx, recorder, "propose_action", {
                **proposal, "action_type": "collect_sensor_data", "water_mm": None, "target_date": None,
                "title": "Confirm soil moisture before irrigating",
                "rationale": "The planner indicates a water need, but the safety policy blocked irrigation: " + "; ".join(result.get("violations") or []),
            })
    await _rules_finish(ctx, recorder)


async def _rules_finish(ctx: AgentContext, recorder: Recorder) -> None:
    if ctx.finish is not None:
        return
    ev = ctx.evidence
    irrigation = ev.get("irrigation") or {}
    drought = ev.get("drought") or {}
    moisture = ev.get("moisture") or {}
    risk_index = DROUGHT_ORDER.index(drought.get("risk_class")) if drought.get("risk_class") in DROUGHT_ORDER else 1
    risk = "critical" if risk_index >= 5 else "high" if risk_index >= 3 or irrigation.get("status") == "irrigate_now" else "moderate" if risk_index >= 1 else "low"
    actions = ctx.accepted_actions
    first = actions[0]["title"] if actions else "Keep monitoring"
    summary = (
        f"Checked evidence freshness, soil moisture ({moisture.get('field_status')}), drought risk ({str(drought.get('risk_class')).replace('_', ' ')}), "
        f"crop health and the 7-day forecast. Irrigation planner status: {str(irrigation.get('status')).replace('_', ' ')}. "
        f"{len(actions)} recommendation(s) issued; AgriLens will re-evaluate after each follow-up window."
    )
    await call_tool(ctx, recorder, "finish_run", {
        "headline": first,
        "risk_level": risk,
        "confidence": min([a["confidence"] for a in actions] or [moisture.get("confidence") or 0.5]),
        "summary_for_farmer": summary,
    })


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def execute_run(run_id: str, field_id: str, trigger: str, context: dict[str, Any], mode: str) -> None:
    recorder = Recorder(run_id)
    usage = {"input_tokens": 0, "output_tokens": 0}
    final_mode = mode
    operation_event_log.record_job_event(
        source="field_resilience_agent", severity="info", state="run_started",
        message=f"Field Resilience Agent started ({trigger}, {mode}).", field_id=field_id, details={"run_id": run_id},
    )
    ctx: Optional[AgentContext] = None
    used_model: Optional[str] = None

    async def cycle() -> None:
        nonlocal ctx, final_mode, used_model
        async with AsyncSessionLocal() as session:
            report = await field_report_service.get_by_field_id(session, field_id)
            if not report:
                raise ValueError("Field report not found")
            ctx = AgentContext(
                field_id=field_id,
                run_id=run_id,
                session=session,
                settings=await store.get_settings(field_id),
                report=report,
                recent_actions=await store.list_actions(field_id=field_id, limit=10),
            )
            if mode == "llm":
                # Preferred provider first; if it is unavailable, the next one takes over the same cycle.
                outcome = "unavailable"
                for provider in llm_providers("agent"):
                    outcome = await CYCLES[provider](ctx, recorder, trigger, context, usage)
                    if outcome != "unavailable":
                        used_model = llm_model_name(provider)
                        break
                    await recorder.step("observe", "reasoning", f"{provider.capitalize()} unavailable; handing over to the next AI provider", {})
                if outcome != "finished" or not ctx.accepted_actions:
                    final_mode = "llm+rules"
                    await run_rules_cycle(ctx, recorder)
            else:
                await run_rules_cycle(ctx, recorder)

            # Tell the field owner about anything worth knowing. Never fails the cycle.
            try:
                await alerts.process_run(ctx, recorder)
            except Exception as exc:
                logger.exception("Farmer warning step failed for run %s", run_id)
                await recorder.step("notify", "error", f"Farmer warning step failed: {type(exc).__name__}: {str(exc)[:200]}", {})

    try:
        # Hard limit so a hung provider or DB call can never leave the run "running" indefinitely.
        try:
            await asyncio.wait_for(cycle(), timeout=store.RUN_TIMEOUT_MINUTES * 60)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Cycle did not finish within {store.RUN_TIMEOUT_MINUTES} minutes") from None

        finish = ctx.finish or {}
        extra = {"model": used_model} if used_model else {}
        await store.update_run(
            run_id,
            status="completed",
            mode=final_mode,
            **extra,
            headline=finish.get("headline"),
            summary=finish.get("summary"),
            risk_level=finish.get("risk_level"),
            confidence=finish.get("confidence"),
            tool_calls=ctx.tool_call_count,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            finished_at=datetime.now(timezone.utc),
        )
        operation_event_log.record_job_event(
            source="field_resilience_agent", severity="success", state="run_completed",
            message=f"Field Resilience Agent proposed {len(ctx.accepted_actions)} action(s).", field_id=field_id,
            details={"run_id": run_id, "mode": final_mode, "headline": finish.get("headline")},
        )
    except Exception as exc:
        logger.exception("Field agent run %s failed", run_id)
        await recorder.step("observe", "error", f"Run failed: {type(exc).__name__}: {str(exc)[:200]}", {})
        await store.update_run(
            run_id, status="failed", mode=final_mode, error=f"{type(exc).__name__}: {str(exc)[:500]}",
            tool_calls=ctx.tool_call_count if ctx else 0, finished_at=datetime.now(timezone.utc),
        )
        operation_event_log.record_job_event(
            source="field_resilience_agent", severity="error", state="run_failed",
            message="Field Resilience Agent run failed.", field_id=field_id, details={"run_id": run_id, "error": str(exc)[:300]},
        )


async def start_run(field_id: str, trigger: str = "manual", context: Optional[dict[str, Any]] = None, force_rules: bool = False) -> dict[str, Any]:
    """Create a run and execute it in the background. Returns {run_id, already_running}."""
    existing = await store.running_run_id(field_id)
    if existing:
        return {"run_id": existing, "already_running": True}
    mode = "llm" if llm_available() and not force_rules else "rules"
    run_id = await store.create_run(field_id, trigger, mode, llm_model_name(purpose="agent") if mode == "llm" else None, GOAL, context or {})
    task = asyncio.create_task(execute_run(run_id, field_id, trigger, context or {}, mode), name=f"field-agent-{run_id}")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"run_id": run_id, "already_running": False, "mode": mode}


async def run_and_wait(field_id: str, trigger: str, context: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Used by the scheduler: run sequentially so background monitoring never floods providers."""
    existing = await store.running_run_id(field_id)
    if existing:
        return existing
    mode = "llm" if llm_available() else "rules"
    run_id = await store.create_run(field_id, trigger, mode, llm_model_name(purpose="agent") if mode == "llm" else None, GOAL, context or {})
    await execute_run(run_id, field_id, trigger, context or {}, mode)
    return run_id

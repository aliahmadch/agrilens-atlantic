"""Pure functions that condense existing service outputs into compact agent evidence.

The analysis engines return rich payloads (10-25 KB each). The agent only needs the
decision-relevant parts, so each summarizer keeps scores, classes, confidence,
data gaps, the factors that drove the result, and per-zone signals.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


CORE_MOISTURE_SOURCES = ("smap_l4", "era5_land", "open_meteo")
STALE_MULTIPLIER = 2.0


def _round(value: Any, digits: int = 2) -> Any:
    return round(value, digits) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _factors(factors: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    return [
        {
            "name": item.get("name"),
            "value": _round(item.get("value")),
            "unit": item.get("unit"),
            "effect": item.get("effect"),
            "message": item.get("message"),
        }
        for item in (factors or [])[:limit]
    ]


def summarize_profile(report: dict[str, Any], settings: dict[str, Any], sample_point_count: Optional[int] = None) -> dict[str, Any]:
    return {
        "field_id": report.get("field_id"),
        "farm_name": report.get("farm_name"),
        "crop_type": report.get("crop_type"),
        "crop_stage": report.get("crop_stage"),
        "soil_type": report.get("soil_type"),
        "irrigation_method": report.get("irrigation_method"),
        "area_hectares": _round(report.get("area_hectares"), 3),
        "farmer_observed_condition": report.get("observed_condition"),
        "latitude": report.get("latitude"),
        "longitude": report.get("longitude"),
        "sample_point_count": sample_point_count,
        "farmer_constraints": {
            "max_application_mm": settings.get("max_application_mm"),
            "min_confidence_for_irrigation": settings.get("min_confidence_for_irrigation"),
            "farmer_notes": settings.get("farmer_notes"),
        },
    }


def summarize_freshness(statuses: dict[str, Any]) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    stale: list[str] = []
    missing: list[str] = []
    newer_available: list[str] = []
    for source, status in (statuses or {}).items():
        status = status or {}
        age = status.get("age_hours")
        cadence = status.get("cadence_hours") or 24
        is_missing = not status.get("last_fetched_at")
        is_stale = is_missing or (isinstance(age, (int, float)) and age > cadence * STALE_MULTIPLIER)
        if is_missing:
            missing.append(source)
        elif is_stale:
            stale.append(source)
        if status.get("refresh_available"):
            newer_available.append(source)
        sources[source] = {
            "age_hours": _round(age, 1),
            "expected_cadence_hours": cadence,
            "data_valid_through": _iso(status.get("stored_source_at")),
            "provider_has_newer_data": bool(status.get("refresh_available")),
            "stale": bool(is_stale),
            "provider_check_error": status.get("provider_check_error"),
        }
    core_stale = [source for source in CORE_MOISTURE_SOURCES if source in stale or source in missing]
    if not stale and not missing:
        verdict = "fresh"
    elif len(core_stale) == len(CORE_MOISTURE_SOURCES):
        verdict = "stale_core_moisture"
    else:
        verdict = "partially_stale"
    return {
        "verdict": verdict,
        "stale_sources": stale,
        "missing_sources": missing,
        "sources_with_newer_provider_data": newer_available,
        "sources": sources,
        "guidance": (
            "Refresh before relying on soil-water decisions."
            if verdict != "fresh" or newer_available
            else "Evidence is within expected provider cadence."
        ),
    }


def summarize_refresh(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "refreshed_sources": [item["source"] for item in results if item.get("fetched")],
        "already_current": [item["source"] for item in results if not item.get("fetched") and not item.get("error")],
        "failed": [{"source": item["source"], "error": str(item.get("error"))[:200]} for item in results if item.get("error")],
        "note": "Analyses run after this refresh use the newly stored provider data.",
    }


def summarize_moisture(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_status": analysis.get("field_status"),
        "field_score_0_100": _round(analysis.get("field_score")),
        "confidence": _round(analysis.get("confidence")),
        "summary": analysis.get("summary"),
        "data_sources_used": analysis.get("data_sources_used"),
        "data_gaps": analysis.get("data_gaps"),
        "factors": _factors(analysis.get("factors") or []),
        "zones": [
            {
                "zone_id": zone.get("zone_id"),
                "status": zone.get("status"),
                "score": _round(zone.get("score")),
                "confidence": _round(zone.get("confidence")),
                "sample_points": len(zone.get("sample_points") or []),
                "main_factors": (zone.get("main_factors") or [])[:3],
                "prediction": zone.get("prediction"),
            }
            for zone in analysis.get("zones") or []
        ],
    }


def summarize_drought(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "risk_class": analysis.get("risk_class"),
        "drought_risk_score_0_100": _round(analysis.get("drought_risk_score")),
        "confidence": _round(analysis.get("confidence")),
        "summary": analysis.get("summary"),
        "category_scores": {key: _round(value) for key, value in (analysis.get("category_scores") or {}).items()},
        "data_gaps": analysis.get("data_gaps"),
        "factors": _factors(analysis.get("factors") or []),
        "zones": [
            {
                "zone_id": zone.get("zone_id"),
                "risk_class": zone.get("risk_class"),
                "score": _round(zone.get("drought_risk_score")),
                "confidence": _round(zone.get("confidence")),
                "centroid": [zone.get("centroid_latitude"), zone.get("centroid_longitude")],
                "main_factors": (zone.get("main_factors") or [])[:3],
                "recommendation": zone.get("recommendation"),
            }
            for zone in analysis.get("zones") or []
        ],
        "engine_recommendation": analysis.get("recommendation"),
    }


def summarize_crop_health(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "health_class": analysis.get("health_class"),
        "health_score_0_100": _round(analysis.get("field_health_score")),
        "stress_score_0_100": _round(analysis.get("field_stress_score")),
        "confidence": _round(analysis.get("confidence")),
        "summary": analysis.get("summary"),
        "data_gaps": analysis.get("data_gaps"),
        "factors": _factors(analysis.get("factors") or []),
        "zones": [
            {
                "zone_id": zone.get("zone_id"),
                "health_class": zone.get("health_class"),
                "health_score": _round(zone.get("health_score")),
                "confidence": _round(zone.get("confidence")),
                "centroid": [zone.get("centroid_latitude"), zone.get("centroid_longitude")],
                "main_factors": (zone.get("main_factors") or [])[:3],
                "data_gaps": (zone.get("data_gaps") or [])[:2],
            }
            for zone in analysis.get("zones") or []
        ],
    }


def summarize_weather(palette: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    upcoming = [day for day in palette.get("days") or [] if str(day.get("date")) >= today]
    days = [
        {
            "date": day.get("date"),
            "condition": day.get("condition_label"),
            "temp_max_c": _round(day.get("temperature_max_c"), 1),
            "temp_min_c": _round(day.get("temperature_min_c"), 1),
            "rain_mm": _round(day.get("precipitation_mm"), 1),
            "rain_probability_percent": _round(day.get("precipitation_probability_percent"), 0),
            "crop_water_demand_mm": _round(day.get("crop_water_demand_mm"), 2),
            "rain_minus_demand_mm": _round(day.get("rain_minus_crop_demand_mm"), 2),
        }
        for day in upcoming[:7]
    ]

    def total(key: str, count: int) -> Optional[float]:
        values = [day[key] for day in days[:count] if isinstance(day.get(key), (int, float))]
        return round(sum(values), 1) if values else None

    return {
        "forecast_generated_at": _iso(palette.get("generated_at")),
        "sources_used": palette.get("sources_used"),
        "current": {
            key: (palette.get("current") or {}).get(key)
            for key in ("condition_label", "temperature_c", "relative_humidity_percent", "wind_speed_ms", "observed_at")
        },
        "days": days,
        "rain_next_48h_mm": total("rain_mm", 2),
        "rain_next_7d_mm": total("rain_mm", 7),
        "crop_demand_next_7d_mm": total("crop_water_demand_mm", 7),
    }


def summarize_irrigation(schedule: dict[str, Any]) -> dict[str, Any]:
    state = schedule.get("current_state") or {}
    return {
        "status": schedule.get("status"),
        "confidence": _round(schedule.get("confidence")),
        "summary": schedule.get("summary"),
        "recommended_date": _iso(schedule.get("recommended_date")),
        "net_irrigation_mm": _round(schedule.get("net_irrigation_mm")),
        "gross_irrigation_mm": _round(schedule.get("gross_irrigation_mm")),
        "full_refill_gross_mm": _round(schedule.get("full_refill_gross_mm")),
        "estimated_volume_m3": _round(schedule.get("estimated_volume_m3"), 1),
        "recommendation_basis": schedule.get("recommendation_basis"),
        "root_zone": {
            "depletion_mm": _round(state.get("root_zone_depletion_mm")),
            "readily_available_water_mm": _round(state.get("readily_available_water_mm")),
            "total_available_water_mm": _round(state.get("total_available_water_mm")),
            "plant_available_water_percent": _round(state.get("plant_available_water_percent")),
            "irrigation_efficiency": state.get("irrigation_efficiency"),
            "moisture_observed_on": _iso(state.get("moisture_observed_on")),
        },
        "projection": [
            {
                "date": _iso(day.get("date")),
                "rain_mm": _round(day.get("precipitation_mm"), 1),
                "effective_rain_mm": _round(day.get("effective_rain_mm"), 1),
                "crop_demand_mm": _round(day.get("crop_water_demand_mm")),
                "depletion_end_mm": _round(day.get("depletion_end_mm")),
                "trigger_exceeded": day.get("threshold_exceeded"),
            }
            for day in (schedule.get("daily_projection") or [])[:8]
        ],
        "data_gaps": schedule.get("data_gaps"),
        "engine_recommendation": schedule.get("recommendation"),
    }


def summarize_trends(moisture_records: list[dict[str, Any]], drought_records: list[dict[str, Any]]) -> dict[str, Any]:
    moisture = [
        {"hour": _iso(item.get("recorded_hour")), "score": _round(item.get("field_score")), "status": item.get("field_status")}
        for item in moisture_records[:12]
    ]
    drought = [
        {"date": _iso(item.get("record_date")), "score": _round(item.get("drought_risk_score")), "risk_class": item.get("risk_class")}
        for item in drought_records[:10]
    ]

    def direction(values: list[Optional[float]], higher_is_worse: bool) -> str:
        clean = [value for value in values if isinstance(value, (int, float))]
        if len(clean) < 2:
            return "insufficient_history"
        newest, oldest = clean[0], clean[-1]
        delta = newest - oldest
        if abs(delta) < 3:
            return "stable"
        worsening = delta > 0 if higher_is_worse else delta < 0
        return "worsening" if worsening else "improving"

    return {
        "moisture_trend": direction([item["score"] for item in moisture], higher_is_worse=False),
        "drought_trend": direction([item["score"] for item in drought], higher_is_worse=True),
        "recent_moisture_scores_newest_first": moisture,
        "recent_drought_scores_newest_first": drought,
    }


def summarize_past_actions(actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "actions_newest_first": [
            {
                "action_type": item.get("action_type"),
                "title": item.get("title"),
                "status": item.get("status"),
                "created_at": _iso(item.get("created_at")),
                "water_mm": _round(item.get("water_mm")),
                "farmer_decision_note": item.get("decision_note"),
                "farmer_field_note": item.get("field_note"),
                "applied_mm": _round(item.get("applied_mm")),
                "outcome": (item.get("outcome") or {}).get("verdict"),
                "outcome_message": (item.get("outcome") or {}).get("message"),
            }
            for item in actions
        ],
        "guidance": "Respect farmer rejections and field notes; do not re-propose a rejected action unless conditions have materially changed.",
    }


def baseline_snapshot(evidence: dict[str, Any]) -> dict[str, Any]:
    """Key metrics captured at proposal time so a later outcome check can measure change."""
    moisture = evidence.get("moisture") or {}
    drought = evidence.get("drought") or {}
    crop = evidence.get("crop_health") or {}
    irrigation = evidence.get("irrigation") or {}
    weather = evidence.get("weather") or {}
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "moisture_score": moisture.get("field_score_0_100"),
        "moisture_status": moisture.get("field_status"),
        "drought_score": drought.get("drought_risk_score_0_100"),
        "drought_class": drought.get("risk_class"),
        "crop_health_score": crop.get("health_score_0_100"),
        "crop_health_class": crop.get("health_class"),
        "root_zone_depletion_mm": (irrigation.get("root_zone") or {}).get("depletion_mm"),
        "irrigation_status": irrigation.get("status"),
        "rain_next_48h_mm": weather.get("rain_next_48h_mm"),
    }


def all_zone_ids(evidence: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("moisture", "drought", "crop_health"):
        for zone in (evidence.get(key) or {}).get("zones") or []:
            if zone.get("zone_id"):
                ids.add(str(zone["zone_id"]))
    return ids

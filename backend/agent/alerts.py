"""Farmer updates: after every monitoring cycle, message the field owner what the check found.

One message per check: the overall result ("All good, no water needed"), any warnings
(rain coming, too wet or dry, drought risk high or rising, crop health low or falling,
a recommendation waiting for a decision) and when the next automatic check is.
Every check sends a message, including repeated "Check now" clicks.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agent import notifier, store, telegram
from agent.tools import HANDLERS, AgentContext
from config.settings import settings as app_settings

logger = logging.getLogger(__name__)

RAIN_ALERT_MM = 10.0
RAIN_URGENT_MM = 25.0
DROUGHT_HIGH_CLASSES = ("high_field_risk", "severe_field_risk", "extreme_field_risk")
DROUGHT_URGENT_CLASSES = ("severe_field_risk", "extreme_field_risk")
DROUGHT_RISE_POINTS = 15.0
CROP_LOW_CLASSES = ("stressed", "critical")
CROP_DROP_POINTS = 10.0
FARM_TZ = ZoneInfo("America/Halifax")  # Atlantic Canada; used for "next check" times in messages

# Evidence the warnings rely on; fetched here if the cycle did not already gather it.
REQUIRED_EVIDENCE = (
    ("moisture", "assess_soil_moisture"),
    ("drought", "assess_drought_risk"),
    ("crop_health", "assess_crop_health"),
    ("weather", "get_weather_outlook"),
)


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _fmt(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.0f}"


def snapshot_from_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    moisture = evidence.get("moisture") or {}
    drought = evidence.get("drought") or {}
    crop = evidence.get("crop_health") or {}
    weather = evidence.get("weather") or {}
    return {
        "moisture_status": moisture.get("field_status"),
        "moisture_score": _num(moisture.get("field_score_0_100")),
        "drought_class": drought.get("risk_class"),
        "drought_score": _num(drought.get("drought_risk_score_0_100")),
        "health_class": crop.get("health_class"),
        "health_score": _num(crop.get("health_score_0_100")),
        "zone_health": {
            str(zone.get("zone_id")): _num(zone.get("health_score"))
            for zone in crop.get("zones") or []
            if zone.get("zone_id") is not None and _num(zone.get("health_score")) is not None
        },
        "rain_next_48h_mm": _num(weather.get("rain_next_48h_mm")),
        "rain_next_7d_mm": _num(weather.get("rain_next_7d_mm")),
    }


def _alert(alert_type: str, severity: str, level: float, text: str, key: Optional[str] = None) -> dict[str, Any]:
    return {"type": alert_type, "key": key or alert_type, "severity": severity, "level": round(level, 1), "text": text}


def decision_text(action: dict[str, Any]) -> str:
    """One short question for the farmer, whatever wording the planner used in its title."""
    kind = action.get("action_type")
    zone = str(action.get("zone_id") or "").replace("_", " ")
    if kind == "irrigate" and action.get("water_mm"):
        return f"Water {action['water_mm']:.0f} mm?"
    if kind == "inspect_zone":
        return f"Please check {zone}." if zone else "Please check the field."
    if kind == "collect_sensor_data":
        return "Please take a soil reading."
    return f"{action.get('title')}?"


def detect_alerts(snapshot: dict[str, Any], previous: Optional[dict[str, Any]], pending_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: which warnings does the current state of the field justify? Urgent ones first."""
    previous = previous or {}
    alerts: list[dict[str, Any]] = []

    rain = snapshot.get("rain_next_48h_mm")
    if rain is not None and rain >= RAIN_ALERT_MM:
        alerts.append(_alert(
            "rain", "urgent" if rain >= RAIN_URGENT_MM else "info", rain,
            f"Rain coming ({rain:.0f} mm in 2 days). No need to water.",
        ))

    status = snapshot.get("moisture_status")
    moisture = snapshot.get("moisture_score")
    if status == "saturated_risk":
        alerts.append(_alert("wet", "urgent", moisture or 100, "Soil is waterlogged. Don't water."))
    elif status == "wet":
        alerts.append(_alert("wet", "info", moisture or 75, "Soil is wet. No need to water."))
    elif status == "too_dry":
        alerts.append(_alert("dry", "urgent", 100 - (moisture or 0), "Soil is very dry. Crop may need water soon."))

    drought_class = snapshot.get("drought_class")
    drought = snapshot.get("drought_score")
    prev_drought = _num(previous.get("drought_score"))
    if drought_class in DROUGHT_HIGH_CLASSES:
        label = drought_class.replace("_field_risk", "")
        alerts.append(_alert(
            "drought", "urgent" if drought_class in DROUGHT_URGENT_CLASSES else "info", drought or 50,
            f"Drought risk is {label}.",
        ))
    elif drought is not None and prev_drought is not None and drought - prev_drought >= DROUGHT_RISE_POINTS and drought_class == "moderate_field_risk":
        alerts.append(_alert("drought", "info", drought, "Drought risk is going up."))

    health_class = snapshot.get("health_class")
    health = snapshot.get("health_score")
    prev_health = _num(previous.get("health_score"))
    zone_drops = sorted(
        (
            (prev_score - score, zone, prev_score, score)
            for zone, score in (snapshot.get("zone_health") or {}).items()
            for prev_score in [_num((previous.get("zone_health") or {}).get(zone))]
            if prev_score is not None and score is not None
        ),
        reverse=True,
    )
    field_drop = (prev_health - health) if health is not None and prev_health is not None else 0
    if health is not None and (health_class in CROP_LOW_CLASSES or field_drop >= CROP_DROP_POINTS):
        text = "Crop health is dropping. Take a look." if field_drop >= CROP_DROP_POINTS else "Crop looks stressed. Take a look."
        alerts.append(_alert("crop_health", "urgent" if health_class == "critical" else "info", 100 - health, text))
    elif zone_drops and zone_drops[0][0] >= CROP_DROP_POINTS:
        drop, zone, before, after = zone_drops[0]
        alerts.append(_alert(
            "crop_health", "info", drop,
            f"Crop in {zone.replace('_', ' ')} looks worse. Take a look.",
        ))

    for action in pending_actions:
        alerts.append(_alert(
            "decision", "urgent" if action.get("priority") == "high" else "info", 1,
            f"{decision_text(action)} Open AgriLens to say yes or no.",
            key=f"decision:{action.get('title')}",
        ))

    alerts.sort(key=lambda item: item["severity"] != "urgent")
    return alerts


def check_status(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """The one-line result of this check, shaped like an alert so it is stored with the message."""
    if any(action.get("requires_approval") for action in actions):
        kind, emoji, text = "attention", "⚠️", "Needs your attention."
    elif any(action.get("action_type") == "wait_for_rain" for action in actions):
        kind, emoji, text = "wait", "🌧️", "No need to water, rain is coming."
    else:
        kind, emoji, text = "ok", "✅", "All good, no water needed."
    return {"type": "status", "key": f"status:{kind}", "severity": "info", "level": 0, "text": text, "emoji": emoji}


def next_check_text(now: datetime, interval_hours: int, checked_at: Optional[datetime] = None) -> str:
    """'tomorrow around 7:10 AM' in farm time; counted from checked_at (default: now)."""
    local_now = now.astimezone(FARM_TZ)
    local_next = ((checked_at or now) + timedelta(hours=interval_hours)).astimezone(FARM_TZ)
    if local_next <= local_now:
        return "soon"
    days = (local_next.date() - local_now.date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else local_next.strftime("%A")
    return f"{day} around {local_next.strftime('%I:%M %p').lstrip('0')}"


def _split(items: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    status = next((item for item in items if item["type"] == "status"), None)
    return status, [item for item in items if item["type"] != "status"]


def compose_sms(farm_name: Optional[str], contact_name: Optional[str], items: list[dict[str, Any]], next_check: Optional[str] = None) -> str:
    """Plain GSM-7 text (no emoji) so each SMS segment holds 160 characters."""
    status, warnings = _split(items)
    lines = [f"AgriLens: {farm_name or 'your field'}"]
    greeting = (f"Hi {contact_name}, " if contact_name else "") + "I just checked your field." if status else (f"Hi {contact_name}," if contact_name else "")
    if greeting:
        lines.append(greeting)
    if status:
        lines.append(status["text"])
    for alert in warnings:
        lines.append(("Urgent: " if alert["severity"] == "urgent" else "") + alert["text"])
    if next_check:
        lines.append(f"Next check: {next_check}.")
    return "\n".join(lines)


EMOJI = {"rain": "🌧️", "wet": "💧", "dry": "🏜️", "drought": "🔥", "crop_health": "🌱", "decision": "✋"}


def compose_telegram(farm_name: Optional[str], contact_name: Optional[str], items: list[dict[str, Any]], next_check: Optional[str] = None) -> str:
    """Telegram HTML: bold result line, emoji per warning, next check time."""
    status, warnings = _split(items)
    name = telegram.escape(contact_name) if contact_name else None
    lines = [f"🌾 <b>AgriLens: {telegram.escape(farm_name or 'your field')}</b>"]
    greeting = (f"Hi {name}, " if name else "") + "I just checked your field." if status else (f"Hi {name}," if name else "")
    if greeting:
        lines.append(greeting)
    if status:
        lines.append(f"{status.get('emoji', '')} <b>{telegram.escape(status['text'])}</b>")
    for alert in warnings:
        prefix = EMOJI.get(alert["type"], "•") + " " + ("<b>Urgent:</b> " if alert["severity"] == "urgent" else "")
        lines.append(prefix + telegram.escape(alert["text"]))
    if next_check:
        lines.append(f"🕖 Next check: {telegram.escape(next_check)}.")
    return "\n".join(lines)


def preferred_channel(settings: dict[str, Any]) -> Optional[str]:
    """Telegram when connected (free), otherwise SMS when a phone is set. None when nothing can deliver."""
    if settings.get("telegram_chat_id") and telegram.configured():
        return "telegram"
    if settings.get("contact_phone") and notifier.sms_configured():
        return "sms"
    return None


async def deliver(
    field_id: str,
    settings: dict[str, Any],
    farm_name: Optional[str],
    alerts: list[dict[str, Any]],
    run_id: Optional[str] = None,
    test_text: Optional[str] = None,
    next_check: Optional[str] = None,
    is_test: bool = False,
) -> dict[str, Any]:
    """Send one message on the preferred channel and log it. Returns the saved message row."""
    channel = preferred_channel(settings) or ("telegram" if settings.get("telegram_chat_id") else "sms")
    name = settings.get("contact_name") or settings.get("telegram_name")
    if channel == "telegram":
        body = compose_telegram(farm_name, name, alerts, next_check) if test_text is None else (
            f"🌾 <b>AgriLens: {telegram.escape(farm_name or 'your field')}</b>\n{telegram.escape(test_text)}"
        )
        delivery = await telegram.send(settings.get("telegram_chat_id"), body)
        recipient = settings.get("telegram_chat_id")
    else:
        body = compose_sms(farm_name, name, alerts, next_check) if test_text is None else f"AgriLens: {farm_name or 'your field'}\n{test_text}"
        delivery = await notifier.send_sms(settings.get("contact_phone"), body)
        recipient = settings.get("contact_phone")
    return await store.create_message(
        field_id, run_id, name, recipient, body,
        [{key: alert[key] for key in ("type", "key", "severity", "level", "text", "emoji") if key in alert} for alert in alerts],
        delivery, is_test=is_test or test_text is not None, channel=channel,
    )


STATUS_EMOJI = {"status:ok": "✅", "status:wait": "🌧️", "status:attention": "⚠️"}


async def latest_update(field_id: str, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], Optional[str]]:
    """The last check's result and next check time, so a test message looks like a real update."""
    messages = await store.list_messages(field_id, 20)
    last = next((m for m in messages if not m.get("is_test") and m.get("alerts") not in (None, [], "[]")), None)
    stored = (last or {}).get("alerts") or []
    if isinstance(stored, str):
        stored = json.loads(stored)
    items = [dict(item) for item in stored] or [check_status([])]
    for item in items:
        if item.get("type") == "status":
            item.setdefault("emoji", STATUS_EMOJI.get(item.get("key"), ""))
    if not (app_settings.AGENT_MONITOR_ENABLED and settings.get("monitor_enabled", True)):
        return items, None
    now = datetime.now(timezone.utc)
    runs = await store.list_runs(field_id, 1)
    checked_at = (runs[0].get("finished_at") or runs[0].get("started_at")) if runs else None
    if isinstance(checked_at, str):
        checked_at = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    return items, next_check_text(now, app_settings.AGENT_MONITOR_INTERVAL_HOURS, checked_at or now)


async def _ensure_evidence(ctx: AgentContext) -> None:
    for key, tool in REQUIRED_EVIDENCE:
        if key in ctx.evidence:
            continue
        try:
            await HANDLERS[tool](ctx, {})
        except Exception:
            logger.exception("Could not gather %s evidence for warnings", key)
            if ctx.session is not None:
                await ctx.session.rollback()


async def process_run(ctx: AgentContext, recorder: Any) -> None:
    """Snapshot the field and send the farmer one update about this check."""
    await _ensure_evidence(ctx)
    snapshot = snapshot_from_evidence(ctx.evidence)
    await store.save_snapshot(ctx.run_id, snapshot)

    settings = ctx.settings or {}
    if not settings.get("alerts_enabled", True):
        await recorder.step("notify", "notify", "Farmer updates are turned off for this field", {"snapshot": snapshot})
        return

    previous = await store.previous_snapshot(ctx.field_id, ctx.run_id)
    pending = [action for action in ctx.accepted_actions if action.get("requires_approval")]
    warnings = detect_alerts(snapshot, previous, pending)
    items = [check_status(ctx.accepted_actions)] + warnings
    details = {"snapshot": snapshot, "compared_with": previous, "update": items}

    autonomous = app_settings.AGENT_MONITOR_ENABLED and settings.get("monitor_enabled", True)
    next_check = next_check_text(datetime.now(timezone.utc), app_settings.AGENT_MONITOR_INTERVAL_HOURS) if autonomous else None
    profile = ctx.evidence.get("profile") or {}
    farm_name = profile.get("farm_name") or (ctx.report or {}).get("farm_name")
    message = await deliver(ctx.field_id, settings, farm_name, items, run_id=ctx.run_id, next_check=next_check)
    who = settings.get("contact_name") or settings.get("telegram_name") or "farmer"
    via = "Telegram" if message["channel"] == "telegram" else "SMS"
    titles = {
        "sent": f"Sent {who} the check result on {via}",
        "not_sent": "Check result saved; no message channel connected",
        "failed": f"{via} message to {who} failed",
    }
    await recorder.step(
        "notify", "notify", titles[message["delivery_status"]],
        {**details, "message": message["body"], "channel": message["channel"], "error": message.get("error"), "message_id": message["message_id"]},
    )

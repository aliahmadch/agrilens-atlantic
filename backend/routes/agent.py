"""HTTP API for the Field Resilience Agent."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent import alerts, chat, gemini, groq, notifier, store, telegram
from agent.outcomes import evaluate_action
from agent.runner import llm_available, llm_model_name, llm_provider, llm_providers, start_run
from agent.tools import field_report_service
from config.postgres import AsyncSessionLocal
from config.settings import settings


router = APIRouter(tags=["field resilience agent"])


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=chat.MAX_MESSAGE_CHARS)
    field_id: Optional[str] = Field(default=None, max_length=32)
    history: list[ChatTurn] = Field(default_factory=list, max_length=30)


class StartRunRequest(BaseModel):
    farmer_message: Optional[str] = Field(default=None, max_length=1000)
    force_rules: bool = False


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: Optional[str] = Field(default=None, max_length=1000)


class CompleteRequest(BaseModel):
    field_note: Optional[str] = Field(default=None, max_length=2000)
    applied_mm: Optional[float] = Field(default=None, ge=0, le=200)


class SettingsRequest(BaseModel):
    max_application_mm: Optional[float] = Field(default=None, gt=0, le=150)
    min_confidence_for_irrigation: Optional[float] = Field(default=None, ge=0, le=1)
    monitor_enabled: Optional[bool] = None
    farmer_notes: Optional[str] = Field(default=None, max_length=1000)
    contact_name: Optional[str] = Field(default=None, max_length=120)
    contact_phone: Optional[str] = Field(default=None, max_length=30, description="E.164, e.g. +19025550123; empty string clears it")
    alerts_enabled: Optional[bool] = None


async def _require_field(field_id: str) -> None:
    async with AsyncSessionLocal() as session:
        if not await field_report_service.get_by_field_id(session, field_id):
            raise HTTPException(status_code=404, detail="Field report not found")


@router.get("/agent/status")
async def agent_status(request: Request) -> dict[str, Any]:
    monitor = getattr(request.app.state, "field_agent_monitor", None)
    listener = getattr(request.app.state, "telegram_listener", None)
    return {
        "llm_enabled": llm_available(),
        "provider": llm_provider(),
        "provider_order": {"chat": llm_providers("chat"), "agent": llm_providers("agent")},
        "llm_last_error": {"groq": groq.status["last_error"], "groq_models": groq.status["model_errors"], "gemini": gemini.status["last_error"]},
        "model": llm_model_name(),
        "mode": "llm" if llm_available() else "rules",
        "monitor": monitor.status() if monitor else {"enabled": False, "running": False},
        "sms_enabled": notifier.sms_configured(),
        "telegram_enabled": telegram.configured(),
        "telegram_bot": await telegram.bot_username(),
        "telegram_listener": listener.status() if listener else {"running": False},
    }


@router.post("/field-reports/{field_id}/agent/runs", status_code=202)
async def create_agent_run(field_id: str, body: Optional[StartRunRequest] = None) -> dict[str, Any]:
    await _require_field(field_id)
    body = body or StartRunRequest()
    context = {"farmer_message": body.farmer_message} if body.farmer_message else {}
    return await start_run(field_id, trigger="manual", context=context, force_rules=body.force_rules)


@router.get("/field-reports/{field_id}/agent/runs")
async def list_agent_runs(field_id: str, limit: int = Query(default=10, ge=1, le=50)) -> list[dict[str, Any]]:
    return await store.list_runs(field_id, limit)


@router.get("/agent/runs/{run_id}")
async def get_agent_run(run_id: str) -> dict[str, Any]:
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.get("/field-reports/{field_id}/agent/actions")
async def list_field_actions(field_id: str, status: Optional[str] = None, limit: int = Query(default=30, ge=1, le=100)) -> list[dict[str, Any]]:
    statuses = [item for item in status.split(",") if item] if status else None
    return await store.list_actions(field_id=field_id, statuses=statuses, limit=limit)


@router.get("/agent/inbox")
async def agent_inbox(limit: int = Query(default=30, ge=1, le=100)) -> list[dict[str, Any]]:
    return await store.list_actions(statuses=list(store.PENDING_STATUSES), limit=limit)


@router.post("/agent/actions/{action_id}/decision")
async def decide_action(action_id: str, body: DecisionRequest) -> dict[str, Any]:
    action = await store.decide_action(action_id, body.decision == "approve", body.note)
    if not action:
        raise HTTPException(status_code=409, detail="Action is not awaiting approval")
    return action


@router.post("/agent/actions/{action_id}/complete")
async def complete_action(action_id: str, body: CompleteRequest) -> dict[str, Any]:
    action = await store.complete_action(action_id, body.field_note, body.applied_mm)
    if not action:
        raise HTTPException(status_code=409, detail="Only approved or monitoring actions can be completed")
    return action


@router.post("/agent/actions/{action_id}/evaluate")
async def evaluate_action_now(action_id: str) -> dict[str, Any]:
    """Run the outcome check immediately instead of waiting for the follow-up time, then re-plan."""
    result = await evaluate_action(action_id, trigger_follow_up=True)
    if not result:
        raise HTTPException(status_code=409, detail="Only approved, completed or monitoring actions can be evaluated")
    return result


@router.get("/field-reports/{field_id}/agent/settings")
async def get_agent_settings(field_id: str) -> dict[str, Any]:
    return await store.get_settings(field_id)


@router.put("/field-reports/{field_id}/agent/settings")
async def update_agent_settings(field_id: str, body: SettingsRequest) -> dict[str, Any]:
    await _require_field(field_id)
    values = body.model_dump(exclude_none=True)
    for key in ("contact_name", "contact_phone"):
        if key in values and not values[key].strip():
            values[key] = None  # empty string clears the field
    if values.get("contact_phone"):
        phone = notifier.normalize_phone(values["contact_phone"])
        if not phone:
            raise HTTPException(status_code=422, detail="Phone number must look like +19025550123")
        values["contact_phone"] = phone
    if values.get("contact_name"):
        values["contact_name"] = values["contact_name"].strip()
    return await store.upsert_settings(field_id, values)


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    recipient = notifier.mask_phone(message.get("recipient")) if message.get("channel") == "sms" else None
    return {**message, "recipient": recipient}


@router.get("/field-reports/{field_id}/agent/messages")
async def list_field_messages(field_id: str, limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    """Warnings the agent sent (or tried to send) to the field owner, newest first."""
    return [_public_message(item) for item in await store.list_messages(field_id, limit)]


@router.post("/field-reports/{field_id}/agent/messages/test")
async def send_test_message(field_id: str) -> dict[str, Any]:
    """Send the field owner the latest check result as a test (Telegram if connected, otherwise SMS)."""
    await _require_field(field_id)
    async with AsyncSessionLocal() as session:
        report = await field_report_service.get_by_field_id(session, field_id)
    field_settings = await store.get_settings(field_id)
    items, next_check = await alerts.latest_update(field_id, field_settings)
    message = await alerts.deliver(field_id, field_settings, report.get("farm_name") or field_id, items, next_check=next_check, is_test=True)
    return _public_message(message)


@router.post("/field-reports/{field_id}/agent/telegram/connect")
async def start_telegram_connect(field_id: str) -> dict[str, Any]:
    """Create a one-time link; when the farmer presses Start in Telegram, their chat is linked to this field."""
    await _require_field(field_id)
    if not telegram.configured():
        raise HTTPException(status_code=503, detail="Telegram bot token is not set on the server")
    username = await telegram.bot_username()
    if not username:
        raise HTTPException(status_code=503, detail="Could not reach the Telegram bot")
    code = telegram.new_connect_code()
    await store.create_telegram_code(field_id, code)
    return {"url": f"https://t.me/{username}?start={code}", "bot": username, "code": code, "expires_in_minutes": 30}


@router.delete("/field-reports/{field_id}/agent/telegram")
async def disconnect_telegram(field_id: str) -> dict[str, Any]:
    await _require_field(field_id)
    await store.disconnect_telegram(field_id)
    return await store.get_settings(field_id)


@router.post("/agent/chat")
async def agent_chat(body: ChatRequest, request: Request) -> dict[str, Any]:
    """Farmer chat: read-only, topic-locked answers about the farmer's fields."""
    client_key = request.client.host if request.client else "unknown"
    if chat.rate_limited(client_key):
        raise HTTPException(status_code=429, detail="Too many messages. Please wait a minute.")
    return await chat.answer(body.message, [turn.model_dump() for turn in body.history], body.field_id)

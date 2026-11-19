"""Persistence for agent runs, reasoning steps, proposed actions and farmer settings.

Every write uses its own short-lived session and commits immediately, so the UI can
poll a run and watch its trace grow while the agent is still working.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text

from config.postgres import AsyncSessionLocal


DEFAULT_SETTINGS: dict[str, Any] = {
    "max_application_mm": 35.0,
    "min_confidence_for_irrigation": 0.55,
    "monitor_enabled": True,
    "farmer_notes": None,
    "contact_name": None,
    "contact_phone": None,
    "alerts_enabled": True,
}

PENDING_STATUSES = ("pending_approval",)
FOLLOW_UP_STATUSES = ("approved", "completed", "monitoring")

# A cycle normally takes 1-3 minutes. Anything still "running" after this is treated as dead.
RUN_TIMEOUT_MINUTES = 15


def _default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def to_json(value: Any) -> str:
    return json.dumps(value, default=_default)


def _row(mapping: Any) -> dict[str, Any]:
    data = dict(mapping)
    for key, value in data.items():
        if isinstance(value, UUID):
            data[key] = str(value)
    return data


# ---------------------------------------------------------------------------
# Runs and steps
# ---------------------------------------------------------------------------

async def create_run(field_id: str, trigger: str, mode: str, model: Optional[str], goal: str, context: dict[str, Any]) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO agent_runs (field_id, trigger, mode, model, goal, context)
                VALUES (:field_id, :trigger, :mode, :model, :goal, CAST(:context AS jsonb))
                RETURNING run_id
                """
            ),
            {"field_id": field_id, "trigger": trigger, "mode": mode, "model": model, "goal": goal, "context": to_json(context)},
        )
        run_id = str(result.scalar_one())
        await session.commit()
        return run_id


async def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "mode", "status", "model", "headline", "summary", "risk_level", "confidence",
        "tool_calls", "input_tokens", "output_tokens", "error", "finished_at",
    }
    assignments = []
    params: dict[str, Any] = {"run_id": run_id}
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Unsupported run field {key}")
        assignments.append(f"{key} = :{key}")
        params[key] = value
    async with AsyncSessionLocal() as session:
        await session.execute(text(f"UPDATE agent_runs SET {', '.join(assignments)} WHERE run_id = :run_id"), params)
        await session.commit()


async def add_step(
    run_id: str,
    seq: int,
    phase: str,
    kind: str,
    title: str,
    content: dict[str, Any],
    tool_name: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO agent_steps (run_id, seq, phase, kind, tool_name, title, content, duration_ms)
                VALUES (:run_id, :seq, :phase, :kind, :tool_name, :title, CAST(:content AS jsonb), :duration_ms)
                """
            ),
            {
                "run_id": run_id,
                "seq": seq,
                "phase": phase,
                "kind": kind,
                "tool_name": tool_name,
                "title": title[:500],
                "content": to_json(content),
                "duration_ms": duration_ms,
            },
        )
        await session.commit()


async def get_run(run_id: str) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT * FROM agent_runs WHERE run_id = CAST(:run_id AS uuid)"), {"run_id": run_id})
        row = result.mappings().first()
        if not row:
            return None
        run = _row(row)
        steps = await session.execute(
            text("SELECT * FROM agent_steps WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq, created_at"),
            {"run_id": run_id},
        )
        run["steps"] = [_row(item) for item in steps.mappings().all()]
        actions = await session.execute(
            text("SELECT * FROM agent_actions WHERE run_id = CAST(:run_id AS uuid) ORDER BY created_at"),
            {"run_id": run_id},
        )
        run["actions"] = [_row(item) for item in actions.mappings().all()]
        return run


async def list_runs(field_id: str, limit: int = 10) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT r.run_id, r.field_id, r.trigger, r.mode, r.status, r.model, r.headline, r.risk_level,
                       r.confidence, r.tool_calls, r.started_at, r.finished_at, r.error,
                       (SELECT count(*) FROM agent_actions a WHERE a.run_id = r.run_id) AS action_count
                FROM agent_runs r
                WHERE r.field_id = :field_id
                ORDER BY r.started_at DESC
                LIMIT :limit
                """
            ),
            {"field_id": field_id, "limit": limit},
        )
        return [_row(item) for item in result.mappings().all()]


async def fail_stale_runs(older_than_minutes: Optional[int], reason: str) -> int:
    """Mark runs stuck in "running" as failed. older_than_minutes=None fails every running run."""
    condition = "" if older_than_minutes is None else "AND started_at < now() - make_interval(mins => :minutes)"
    params: dict[str, Any] = {"reason": reason}
    if older_than_minutes is not None:
        params["minutes"] = older_than_minutes
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                f"""
                UPDATE agent_runs
                SET status = 'failed', error = :reason, finished_at = now()
                WHERE status = 'running' {condition}
                """
            ),
            params,
        )
        await session.commit()
        return result.rowcount or 0


async def running_run_id(field_id: str) -> Optional[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT run_id FROM agent_runs
                WHERE field_id = :field_id AND status = 'running'
                  AND started_at > now() - make_interval(mins => :timeout_minutes)
                ORDER BY started_at DESC LIMIT 1
                """
            ),
            {"field_id": field_id, "timeout_minutes": RUN_TIMEOUT_MINUTES},
        )
        value = result.scalar()
        return str(value) if value else None


async def last_run_started_at(field_id: str) -> Optional[datetime]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT max(started_at) FROM agent_runs WHERE field_id = :field_id AND status IN ('running', 'completed')"),
            {"field_id": field_id},
        )
        return result.scalar()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def create_action(run_id: str, field_id: str, action: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    status = "pending_approval" if action["requires_approval"] else "monitoring"
    due_at = None if action["requires_approval"] else now + timedelta(hours=action["follow_up_hours"])
    async with AsyncSessionLocal() as session:
        # The newest cycle is the current plan: it replaces every still-unanswered recommendation from older
        # cycles, so the farmer never sees "all good" next to a stale request it already resolved.
        await session.execute(
            text(
                """
                UPDATE agent_actions SET status = 'superseded', updated_at = now()
                WHERE field_id = :field_id
                  AND status IN ('pending_approval', 'monitoring') AND run_id <> CAST(:run_id AS uuid)
                """
            ),
            {"field_id": field_id, "run_id": run_id},
        )
        result = await session.execute(
            text(
                """
                INSERT INTO agent_actions (
                    run_id, field_id, action_type, title, rationale, evidence, assumptions, confidence, priority,
                    water_mm, volume_m3, target_date, zone_id, requires_approval, status, policy_checks, baseline,
                    follow_up_hours, follow_up_due_at
                ) VALUES (
                    CAST(:run_id AS uuid), :field_id, :action_type, :title, :rationale, CAST(:evidence AS jsonb),
                    CAST(:assumptions AS jsonb), :confidence, :priority, :water_mm, :volume_m3, :target_date, :zone_id,
                    :requires_approval, :status, CAST(:policy_checks AS jsonb), CAST(:baseline AS jsonb),
                    :follow_up_hours, :follow_up_due_at
                )
                RETURNING *
                """
            ),
            {
                "run_id": run_id,
                "field_id": field_id,
                "action_type": action["action_type"],
                "title": action["title"],
                "rationale": action["rationale"],
                "evidence": to_json(action.get("evidence") or []),
                "assumptions": to_json(action.get("assumptions") or []),
                "confidence": action["confidence"],
                "priority": action["priority"],
                "water_mm": action.get("water_mm"),
                "volume_m3": action.get("volume_m3"),
                "target_date": action.get("target_date"),
                "zone_id": action.get("zone_id"),
                "requires_approval": action["requires_approval"],
                "status": status,
                "policy_checks": to_json(action.get("policy_checks") or []),
                "baseline": to_json(action.get("baseline") or {}),
                "follow_up_hours": action["follow_up_hours"],
                "follow_up_due_at": due_at,
            },
        )
        row = _row(result.mappings().one())
        await session.commit()
        return row


async def get_action(action_id: str) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM agent_actions WHERE action_id = CAST(:action_id AS uuid)"),
            {"action_id": action_id},
        )
        row = result.mappings().first()
        return _row(row) if row else None


async def list_actions(field_id: Optional[str] = None, statuses: Optional[list[str]] = None, limit: int = 30) -> list[dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {"limit": limit}
    if field_id:
        clauses.append("a.field_id = :field_id")
        params["field_id"] = field_id
    if statuses:
        clauses.append("a.status = ANY(:statuses)")
        params["statuses"] = statuses
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                f"""
                SELECT a.*, f.farm_name
                FROM agent_actions a
                JOIN field_reports f ON f.field_id = a.field_id
                {where}
                ORDER BY a.created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        return [_row(item) for item in result.mappings().all()]


async def decide_action(action_id: str, approve: bool, note: Optional[str]) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                UPDATE agent_actions
                SET status = CASE WHEN :approve THEN 'approved' ELSE 'rejected' END,
                    decision_note = :note,
                    decided_at = now(),
                    follow_up_due_at = CASE WHEN :approve THEN now() + make_interval(hours => follow_up_hours) ELSE NULL END,
                    updated_at = now()
                WHERE action_id = CAST(:action_id AS uuid) AND status = 'pending_approval'
                RETURNING *
                """
            ),
            {"action_id": action_id, "approve": approve, "note": note},
        )
        row = result.mappings().first()
        await session.commit()
        return _row(row) if row else None


async def complete_action(action_id: str, field_note: Optional[str], applied_mm: Optional[float]) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                UPDATE agent_actions
                SET status = 'completed', field_note = :field_note, applied_mm = :applied_mm,
                    completed_at = now(), updated_at = now()
                WHERE action_id = CAST(:action_id AS uuid) AND status IN ('approved', 'monitoring')
                RETURNING *
                """
            ),
            {"action_id": action_id, "field_note": field_note, "applied_mm": applied_mm},
        )
        row = result.mappings().first()
        await session.commit()
        return _row(row) if row else None


async def record_outcome(action_id: str, outcome: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE agent_actions
                SET status = 'evaluated', outcome = CAST(:outcome AS jsonb), evaluated_at = now(), updated_at = now()
                WHERE action_id = CAST(:action_id AS uuid)
                """
            ),
            {"action_id": action_id, "outcome": to_json(outcome)},
        )
        await session.commit()


async def due_follow_ups(limit: int = 20) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT * FROM agent_actions
                WHERE status = ANY(:statuses) AND follow_up_due_at IS NOT NULL AND follow_up_due_at <= now()
                ORDER BY follow_up_due_at
                LIMIT :limit
                """
            ),
            {"statuses": list(FOLLOW_UP_STATUSES), "limit": limit},
        )
        return [_row(item) for item in result.mappings().all()]


# ---------------------------------------------------------------------------
# Farmer settings
# ---------------------------------------------------------------------------

async def get_settings(field_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM agent_field_settings WHERE field_id = :field_id"),
            {"field_id": field_id},
        )
        row = result.mappings().first()
    data = {"field_id": field_id, **DEFAULT_SETTINGS, "telegram_chat_id": None, "telegram_name": None}
    if row:
        data.update({
            key: value for key, value in dict(row).items()
            if key in DEFAULT_SETTINGS or key in ("updated_at", "telegram_chat_id", "telegram_name")
        })
    return data


async def upsert_settings(field_id: str, values: dict[str, Any]) -> dict[str, Any]:
    merged = {**(await get_settings(field_id)), **{k: v for k, v in values.items() if k in DEFAULT_SETTINGS}}
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO agent_field_settings (
                    field_id, max_application_mm, min_confidence_for_irrigation, monitor_enabled, farmer_notes,
                    contact_name, contact_phone, alerts_enabled, updated_at
                )
                VALUES (
                    :field_id, :max_application_mm, :min_confidence_for_irrigation, :monitor_enabled, :farmer_notes,
                    :contact_name, :contact_phone, :alerts_enabled, now()
                )
                ON CONFLICT (field_id) DO UPDATE SET
                    max_application_mm = EXCLUDED.max_application_mm,
                    min_confidence_for_irrigation = EXCLUDED.min_confidence_for_irrigation,
                    monitor_enabled = EXCLUDED.monitor_enabled,
                    farmer_notes = EXCLUDED.farmer_notes,
                    contact_name = EXCLUDED.contact_name,
                    contact_phone = EXCLUDED.contact_phone,
                    alerts_enabled = EXCLUDED.alerts_enabled,
                    updated_at = now()
                """
            ),
            {"field_id": field_id, **{key: merged[key] for key in DEFAULT_SETTINGS}},
        )
        await session.commit()
    return await get_settings(field_id)


# ---------------------------------------------------------------------------
# Farmer warnings: condition snapshots and the SMS message log
# ---------------------------------------------------------------------------

async def save_snapshot(run_id: str, snapshot: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE agent_runs SET snapshot = CAST(:snapshot AS jsonb) WHERE run_id = CAST(:run_id AS uuid)"),
            {"run_id": run_id, "snapshot": to_json(snapshot)},
        )
        await session.commit()


async def previous_snapshot(field_id: str, exclude_run_id: str, min_age_hours: int = 20) -> Optional[dict[str, Any]]:
    """The newest snapshot at least min_age_hours old (a day-over-day comparison); else the oldest one available."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT snapshot, started_at FROM agent_runs
                WHERE field_id = :field_id AND snapshot IS NOT NULL AND run_id <> CAST(:run_id AS uuid)
                ORDER BY (started_at < now() - make_interval(hours => :min_age)) DESC,
                         CASE WHEN started_at < now() - make_interval(hours => :min_age) THEN started_at END DESC,
                         started_at ASC
                LIMIT 1
                """
            ),
            {"field_id": field_id, "run_id": exclude_run_id, "min_age": min_age_hours},
        )
        row = result.mappings().first()
    if not row:
        return None
    return {**row["snapshot"], "taken_at": row["started_at"].isoformat()}


async def recent_messages(field_id: str, hours: int = 24, statuses: tuple[str, ...] = ("sent", "not_sent")) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT * FROM agent_messages
                WHERE field_id = :field_id AND is_test = FALSE AND delivery_status = ANY(:statuses)
                  AND created_at > now() - make_interval(hours => :hours)
                ORDER BY created_at DESC
                """
            ),
            {"field_id": field_id, "hours": hours, "statuses": list(statuses)},
        )
        return [_row(item) for item in result.mappings().all()]


async def create_telegram_code(field_id: str, code: str, minutes: int = 30) -> None:
    await upsert_settings(field_id, {})  # make sure the settings row exists
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE agent_field_settings
                SET telegram_connect_code = :code, telegram_code_expires_at = now() + make_interval(mins => :minutes)
                WHERE field_id = :field_id
                """
            ),
            {"field_id": field_id, "code": code, "minutes": minutes},
        )
        await session.commit()


async def connect_telegram(code: str, chat_id: str, name: Optional[str]) -> Optional[dict[str, Any]]:
    """Link a Telegram chat to the field that issued this unexpired one-time code."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                UPDATE agent_field_settings s
                SET telegram_chat_id = :chat_id, telegram_name = :name,
                    telegram_connect_code = NULL, telegram_code_expires_at = NULL, updated_at = now()
                FROM field_reports f
                WHERE f.field_id = s.field_id AND s.telegram_connect_code = :code AND s.telegram_code_expires_at > now()
                RETURNING s.field_id, f.farm_name
                """
            ),
            {"code": code, "chat_id": chat_id, "name": (name or "")[:120] or None},
        )
        row = result.mappings().first()
        await session.commit()
    return dict(row) if row else None


async def disconnect_telegram(field_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE agent_field_settings SET telegram_chat_id = NULL, telegram_name = NULL, updated_at = now() WHERE field_id = :field_id"),
            {"field_id": field_id},
        )
        await session.commit()


async def disconnect_telegram_chat(chat_id: str) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE agent_field_settings SET telegram_chat_id = NULL, telegram_name = NULL, updated_at = now() WHERE telegram_chat_id = :chat_id"),
            {"chat_id": chat_id},
        )
        await session.commit()
        return result.rowcount or 0


async def create_message(
    field_id: str,
    run_id: Optional[str],
    recipient_name: Optional[str],
    recipient: Optional[str],
    body: str,
    alerts: list[dict[str, Any]],
    delivery: dict[str, Any],
    is_test: bool = False,
    channel: str = "sms",
) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO agent_messages (
                    field_id, run_id, channel, recipient_name, recipient, body, alerts, is_test,
                    delivery_status, provider_message_id, error
                )
                VALUES (
                    :field_id, CAST(:run_id AS uuid), :channel, :recipient_name, :recipient, :body, CAST(:alerts AS jsonb), :is_test,
                    :delivery_status, :provider_message_id, :error
                )
                RETURNING *
                """
            ),
            {
                "field_id": field_id,
                "run_id": run_id,
                "channel": channel,
                "recipient_name": recipient_name,
                "recipient": recipient,
                "body": body,
                "alerts": to_json(alerts),
                "is_test": is_test,
                "delivery_status": delivery["status"],
                "provider_message_id": delivery.get("provider_message_id"),
                "error": delivery.get("error"),
            },
        )
        row = _row(result.mappings().one())
        await session.commit()
        return row


async def list_messages(field_id: str, limit: int = 20) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM agent_messages WHERE field_id = :field_id ORDER BY created_at DESC LIMIT :limit"),
            {"field_id": field_id, "limit": limit},
        )
        return [_row(item) for item in result.mappings().all()]

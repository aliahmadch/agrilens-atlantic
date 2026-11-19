"""Idempotent DDL for the Field Resilience Agent tables.

Shared by the Alembic migration (20261111_0015) and the startup guard so the
agent works on a database that has not run `alembic upgrade head` yet.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


AGENT_DDL_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        field_id VARCHAR(32) NOT NULL REFERENCES field_reports(field_id) ON DELETE CASCADE,
        trigger VARCHAR(40) NOT NULL DEFAULT 'manual',
        mode VARCHAR(20) NOT NULL DEFAULT 'llm',
        status VARCHAR(20) NOT NULL DEFAULT 'running',
        model VARCHAR(80),
        goal TEXT,
        headline TEXT,
        summary TEXT,
        risk_level VARCHAR(20),
        confidence DOUBLE PRECISION,
        tool_calls INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        context JSONB NOT NULL DEFAULT '{}'::jsonb,
        error TEXT,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_field_started ON agent_runs (field_id, started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_steps (
        step_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        run_id UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        phase VARCHAR(20) NOT NULL,
        kind VARCHAR(20) NOT NULL,
        tool_name VARCHAR(60),
        title TEXT NOT NULL,
        content JSONB NOT NULL DEFAULT '{}'::jsonb,
        duration_ms INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_steps_run_seq ON agent_steps (run_id, seq)",
    """
    CREATE TABLE IF NOT EXISTS agent_actions (
        action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        run_id UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        field_id VARCHAR(32) NOT NULL REFERENCES field_reports(field_id) ON DELETE CASCADE,
        action_type VARCHAR(40) NOT NULL,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        assumptions JSONB NOT NULL DEFAULT '[]'::jsonb,
        confidence DOUBLE PRECISION NOT NULL,
        priority VARCHAR(10) NOT NULL DEFAULT 'medium',
        water_mm DOUBLE PRECISION,
        volume_m3 DOUBLE PRECISION,
        target_date DATE,
        zone_id VARCHAR(40),
        requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
        status VARCHAR(30) NOT NULL,
        policy_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
        baseline JSONB NOT NULL DEFAULT '{}'::jsonb,
        follow_up_hours INTEGER NOT NULL DEFAULT 24,
        follow_up_due_at TIMESTAMPTZ,
        decision_note TEXT,
        decided_at TIMESTAMPTZ,
        applied_mm DOUBLE PRECISION,
        field_note TEXT,
        completed_at TIMESTAMPTZ,
        outcome JSONB,
        evaluated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_actions_field_created ON agent_actions (field_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_agent_actions_status_due ON agent_actions (status, follow_up_due_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_field_settings (
        field_id VARCHAR(32) PRIMARY KEY REFERENCES field_reports(field_id) ON DELETE CASCADE,
        max_application_mm DOUBLE PRECISION NOT NULL DEFAULT 35,
        min_confidence_for_irrigation DOUBLE PRECISION NOT NULL DEFAULT 0.55,
        monitor_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        farmer_notes TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]

# Farmer warnings (revision 20261112_0016): contact details, per-run condition snapshots and a message log.
ALERT_DDL_STATEMENTS: list[str] = [
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS contact_name VARCHAR(120)",
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS contact_phone VARCHAR(20)",
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS alerts_enabled BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS snapshot JSONB",
    """
    CREATE TABLE IF NOT EXISTS agent_messages (
        message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        field_id VARCHAR(32) NOT NULL REFERENCES field_reports(field_id) ON DELETE CASCADE,
        run_id UUID REFERENCES agent_runs(run_id) ON DELETE SET NULL,
        channel VARCHAR(10) NOT NULL DEFAULT 'sms',
        recipient_name VARCHAR(120),
        recipient VARCHAR(20),
        body TEXT NOT NULL,
        alerts JSONB NOT NULL DEFAULT '[]'::jsonb,
        is_test BOOLEAN NOT NULL DEFAULT FALSE,
        delivery_status VARCHAR(20) NOT NULL,
        provider_message_id VARCHAR(64),
        error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_messages_field_created ON agent_messages (field_id, created_at DESC)",
]

ALERT_DROP_STATEMENTS: list[str] = [
    "DROP TABLE IF EXISTS agent_messages",
    "ALTER TABLE agent_runs DROP COLUMN IF EXISTS snapshot",
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS alerts_enabled",
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS contact_phone",
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS contact_name",
]

# Telegram delivery (revision 20261112_0017): the chat linked to each field and the one-time connect code.
TELEGRAM_DDL_STATEMENTS: list[str] = [
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(32)",
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS telegram_name VARCHAR(120)",
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS telegram_connect_code VARCHAR(40)",
    "ALTER TABLE agent_field_settings ADD COLUMN IF NOT EXISTS telegram_code_expires_at TIMESTAMPTZ",
]

TELEGRAM_DROP_STATEMENTS: list[str] = [
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS telegram_code_expires_at",
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS telegram_connect_code",
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS telegram_name",
    "ALTER TABLE agent_field_settings DROP COLUMN IF EXISTS telegram_chat_id",
]

AGENT_DROP_STATEMENTS: list[str] = [
    "DROP TABLE IF EXISTS agent_field_settings",
    "DROP TABLE IF EXISTS agent_actions",
    "DROP TABLE IF EXISTS agent_steps",
    "DROP TABLE IF EXISTS agent_runs",
]


async def ensure_agent_schema(session: AsyncSession) -> None:
    for statement in AGENT_DDL_STATEMENTS + ALERT_DDL_STATEMENTS + TELEGRAM_DDL_STATEMENTS:
        await session.execute(text(statement))
    # Runs execute as in-process background tasks, so none survive a restart (e.g. uvicorn --reload).
    # Any run still marked "running" at startup was interrupted and would otherwise look busy forever.
    await session.execute(
        text(
            """
            UPDATE agent_runs
            SET status = 'failed', error = 'Interrupted by server restart.', finished_at = now()
            WHERE status = 'running'
            """
        )
    )
    await session.commit()

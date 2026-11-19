"""add farmer contact, run snapshots and SMS message log for the field agent

Revision ID: 20261112_0016
Revises: 20261111_0015
Create Date: 2026-11-12 12:00:00

"""
from typing import Sequence, Union

from alembic import op


revision: str = "20261112_0016"
down_revision: Union[str, None] = "20261111_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Imported lazily so the statements stay identical to the startup guard in agent/schema.py.
    from agent.schema import ALERT_DDL_STATEMENTS

    for statement in ALERT_DDL_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    from agent.schema import ALERT_DROP_STATEMENTS

    for statement in ALERT_DROP_STATEMENTS:
        op.execute(statement)

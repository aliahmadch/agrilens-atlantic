"""create field resilience agent tables

Revision ID: 20261111_0015
Revises: 20260825_0014
Create Date: 2026-11-11 12:00:00

"""
from typing import Sequence, Union

from alembic import op


revision: str = "20261111_0015"
down_revision: Union[str, None] = "20260825_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Imported lazily so the statements stay identical to the startup guard in agent/schema.py.
    from agent.schema import AGENT_DDL_STATEMENTS

    for statement in AGENT_DDL_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    from agent.schema import AGENT_DROP_STATEMENTS

    for statement in AGENT_DROP_STATEMENTS:
        op.execute(statement)

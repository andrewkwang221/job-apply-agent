"""Add reject_code and reject_detail to jobs

Revision ID: c9e4a1b70f33
Revises: b16bb02fc7ed
Create Date: 2026-09-11 00:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e4a1b70f33"
down_revision: Union[str, Sequence[str], None] = "b16bb02fc7ed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("jobs")}
    if "reject_code" not in cols:
        op.add_column("jobs", sa.Column("reject_code", sa.String(), nullable=True))
    if "reject_detail" not in cols:
        op.add_column("jobs", sa.Column("reject_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("jobs")}
    if "reject_detail" in cols:
        op.drop_column("jobs", "reject_detail")
    if "reject_code" in cols:
        op.drop_column("jobs", "reject_code")

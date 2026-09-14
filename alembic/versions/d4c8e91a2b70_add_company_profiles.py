"""add company_profiles

Revision ID: d4c8e91a2b70
Revises: b16bb02fc7ed
Create Date: 2026-09-14 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4c8e91a2b70"
down_revision: Union[str, Sequence[str], None] = "b16bb02fc7ed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name_key", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("website_url", sa.String(), nullable=True),
        sa.Column("website_host", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("analysis", sa.Text(), nullable=True),
        sa.Column("sources", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name_key"),
        sa.UniqueConstraint("website_host"),
    )


def downgrade() -> None:
    op.drop_table("company_profiles")

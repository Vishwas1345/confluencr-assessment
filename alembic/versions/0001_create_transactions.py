"""create transactions table

Revision ID: 0001
Revises:
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("source_account", sa.Text(), nullable=False),
        sa.Column("destination_account", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "processing_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        sa.CheckConstraint(
            "status IN ('PROCESSING', 'PROCESSED')",
            name="ck_transactions_status_valid",
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
    )
    op.create_index(
        "ix_transactions_processing_reconcile",
        "transactions",
        ["status", "dispatched_at", "lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.create_index(
        "ix_transactions_status_created_at",
        "transactions",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_status_created_at", table_name="transactions")
    op.drop_index(
        "ix_transactions_processing_reconcile",
        table_name="transactions",
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.drop_table("transactions")

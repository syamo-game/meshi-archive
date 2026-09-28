from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260928_01"
down_revision: str = "20260920_01"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The legacy baseline also creates tables from the current model metadata.
    if "login_attempts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "login_attempts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started", sa.BigInteger(), nullable=False),
        sa.Column("blocked_until", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_login_attempts_window_started", "login_attempts", ["window_started"])


def downgrade() -> None:
    op.drop_table("login_attempts")

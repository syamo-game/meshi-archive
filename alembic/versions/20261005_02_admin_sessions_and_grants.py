from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20261005_02"
down_revision: str = "20261005_01"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "admin_sessions" not in existing:
        op.create_table(
            "admin_sessions",
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column("auth_method", sa.String(16), nullable=False),
            sa.Column("actor_discord_user_id", sa.String(20), nullable=True),
            sa.Column("credential_binding", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "discord_viewer_grant_events" not in existing:
        op.create_table(
            "discord_viewer_grant_events",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("discord_user_id", sa.String(20), nullable=False),
            sa.Column("actor_method", sa.String(16), nullable=False),
            sa.Column("actor_discord_user_id", sa.String(20), nullable=True),
            sa.Column("actor_session_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    indexes = {row["name"] for row in sa.inspect(op.get_bind()).get_indexes("discord_viewer_grant_events")}
    if "ix_discord_viewer_grant_events_discord_user_id" not in indexes:
        op.create_index("ix_discord_viewer_grant_events_discord_user_id", "discord_viewer_grant_events", ["discord_user_id"])


def downgrade() -> None:
    events = sa.table("discord_viewer_grant_events", sa.column("id"))
    if op.get_bind().execute(sa.select(events.c.id).limit(1)).first():
        raise RuntimeError("Export grant audit events before downgrading; they cannot be recreated.")
    op.drop_index("ix_discord_viewer_grant_events_discord_user_id", table_name="discord_viewer_grant_events")
    op.drop_table("discord_viewer_grant_events")
    op.drop_table("admin_sessions")

"""Add revocable Discord grants and change audit."""
from __future__ import annotations

import secrets
from alembic import op
import sqlalchemy as sa

revision: str = "20261006_01"
down_revision: str = "20261005_02"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("discord_viewers")}
    if "generation" not in columns:
        op.add_column("discord_viewers", sa.Column("generation", sa.String(32), nullable=True))
    viewers = sa.table("discord_viewers", sa.column("discord_user_id", sa.String(20)), sa.column("generation", sa.String(32)))
    connection = op.get_bind()
    for user_id in connection.execute(sa.select(viewers.c.discord_user_id).where(viewers.c.generation.is_(None))).scalars():
        connection.execute(viewers.update().where(viewers.c.discord_user_id == user_id).values(generation=secrets.token_hex(16)))
    with op.batch_alter_table("discord_viewers") as batch:
        batch.alter_column("generation", existing_type=sa.String(32), nullable=False)
    event_columns = {column["name"] for column in sa.inspect(connection).get_columns("discord_viewer_grant_events")}
    if "action" not in event_columns:
        op.add_column("discord_viewer_grant_events", sa.Column("action", sa.String(16), nullable=False, server_default="grant"))
    if "replacement_user_id" not in event_columns:
        op.add_column("discord_viewer_grant_events", sa.Column("replacement_user_id", sa.String(20), nullable=True))


def downgrade() -> None:
    events = sa.table("discord_viewer_grant_events", sa.column("action"))
    if op.get_bind().execute(sa.select(events.c.action).where(events.c.action != "grant").limit(1)).first():
        raise RuntimeError("Export and preserve access change audit before downgrading.")
    with op.batch_alter_table("discord_viewer_grant_events") as batch:
        batch.drop_column("replacement_user_id")
        batch.drop_column("action")
    with op.batch_alter_table("discord_viewers") as batch:
        batch.drop_column("generation")

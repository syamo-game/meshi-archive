"""Store editable Discord Web roles and their audit trail."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "20261006_02"
down_revision: str = "20261006_01"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Earlier migrations may create tables from current metadata.
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("discord_viewers")}
    if "is_admin" not in columns:
        op.add_column("discord_viewers", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    event_columns = {column["name"] for column in sa.inspect(connection).get_columns("discord_viewer_grant_events")}
    for name in ("previous_role", "new_role"):
        if name not in event_columns:
            op.add_column("discord_viewer_grant_events", sa.Column(name, sa.String(16), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    viewers = sa.table("discord_viewers", sa.column("is_admin", sa.Boolean()))
    events = sa.table("discord_viewer_grant_events", sa.column("previous_role"), sa.column("new_role"))
    if connection.execute(sa.select(viewers.c.is_admin).where(viewers.c.is_admin.is_(True)).limit(1)).first():
        raise RuntimeError("Restore administrator access in configuration before downgrading user roles.")
    if connection.execute(sa.select(events.c.new_role).where(
        sa.or_(events.c.previous_role.is_not(None), events.c.new_role.is_not(None)),
    ).limit(1)).first():
        raise RuntimeError("Export and preserve access change audit before downgrading user roles.")
    with op.batch_alter_table("discord_viewer_grant_events") as batch:
        batch.drop_column("new_role")
        batch.drop_column("previous_role")
    with op.batch_alter_table("discord_viewers") as batch:
        batch.drop_column("is_admin")

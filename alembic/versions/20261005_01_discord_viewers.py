from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20261005_01"
down_revision: str = "20260928_01"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The initial migration also creates tables from current model metadata.
    if "discord_viewers" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "discord_viewers",
        sa.Column("discord_user_id", sa.String(20), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    viewers = sa.table("discord_viewers", sa.column("discord_user_id"))
    if op.get_bind().execute(sa.select(viewers.c.discord_user_id).limit(1)).first():
        raise RuntimeError(
            "Cannot downgrade while Discord viewer grants exist; "
            "review and remove grants explicitly before dropping discord_viewers."
        )
    op.drop_table("discord_viewers")

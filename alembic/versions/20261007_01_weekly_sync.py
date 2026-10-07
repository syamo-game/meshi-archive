"""Keep weekly channel sync attempt timestamps."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "20261007_01"
down_revision: str = "20261006_02"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sync_states")}
    for name in ("last_weekly_sync_started_at", "last_weekly_sync_completed_at"):
        if name not in columns:
            op.add_column("sync_states", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sync_states") as batch:
        batch.drop_column("last_weekly_sync_completed_at")
        batch.drop_column("last_weekly_sync_started_at")

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260920_01"
down_revision: str = "20260830_01"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _shop_indexes(bind: sa.Connection) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(bind).get_indexes("shops")
        if index["name"] is not None
    }


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("shops", "import_rows"):
        if "image_key" not in _columns(bind, table_name):
            op.add_column(table_name, sa.Column("image_key", sa.String(64), nullable=True))
    if "ix_shops_image_key" not in _shop_indexes(bind):
        op.create_index("ix_shops_image_key", "shops", ["image_key"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_shops_image_key" in _shop_indexes(bind):
        op.drop_index("ix_shops_image_key", table_name="shops")
    for table_name in ("import_rows", "shops"):
        if "image_key" in _columns(bind, table_name):
            with op.batch_alter_table(table_name) as batch:
                batch.drop_column("image_key")

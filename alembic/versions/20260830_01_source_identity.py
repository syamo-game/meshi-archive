from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from services.source_identity import source_asset_fingerprint, source_asset_identity


revision = "20260830_01"
down_revision = "20260805_01"
branch_labels = None
depends_on = None


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _indexes(bind: sa.Connection, table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(bind).get_indexes(table_name)
        if index["name"] is not None
    }


def _checks(bind: sa.Connection, table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints(table_name)
        if constraint["name"] is not None
    }


def _foreign_keys(bind: sa.Connection, table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(bind).get_foreign_keys(table_name)
        if constraint["name"] is not None
    }


def _backfill_source_assets(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, kind, url, title, description, extracted_text "
            "FROM source_assets ORDER BY id"
        )
    ).mappings()
    for row in rows:
        identity = source_asset_identity(row["url"])
        normalized_url = identity.normalized_url if identity is not None else row["url"]
        bind.execute(
            sa.text(
                "UPDATE source_assets SET "
                "source_service=:source_service, source_item_id=:source_item_id, "
                "normalized_url=:normalized_url, content_fingerprint=:content_fingerprint "
                "WHERE id=:asset_id"
            ),
            {
                "asset_id": row["id"],
                "source_service": (
                    identity.source_service.value
                    if identity is not None and identity.source_service is not None
                    else None
                ),
                "source_item_id": (
                    identity.source_item_id if identity is not None else None
                ),
                "normalized_url": normalized_url,
                "content_fingerprint": source_asset_fingerprint(
                    kind=row["kind"],
                    normalized_url=normalized_url,
                    title=row["title"],
                    description=row["description"],
                    extracted_text=row["extracted_text"],
                ),
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    asset_columns = _columns(bind, "source_assets")
    asset_checks = _checks(bind, "source_assets")
    with op.batch_alter_table("source_assets") as batch:
        if "source_service" not in asset_columns:
            batch.add_column(sa.Column("source_service", sa.String(32)))
        if "source_item_id" not in asset_columns:
            batch.add_column(sa.Column("source_item_id", sa.String(255)))
        if "normalized_url" not in asset_columns:
            batch.add_column(sa.Column("normalized_url", sa.Text()))
        if "content_fingerprint" not in asset_columns:
            batch.add_column(sa.Column("content_fingerprint", sa.String(64)))
        if "ck_source_assets_identity_pair" not in asset_checks:
            batch.create_check_constraint(
                "ck_source_assets_identity_pair",
                "(source_service IS NULL AND source_item_id IS NULL) OR "
                "(source_service IS NOT NULL AND source_item_id IS NOT NULL)",
            )

    _backfill_source_assets(bind)

    asset_indexes = _indexes(bind, "source_assets")
    if "ix_source_assets_source_identity" not in asset_indexes:
        op.create_index(
            "ix_source_assets_source_identity",
            "source_assets",
            ["source_service", "source_item_id", "normalized_url"],
        )
    if "ix_source_assets_normalized_url" not in asset_indexes:
        op.create_index(
            "ix_source_assets_normalized_url",
            "source_assets",
            ["normalized_url"],
        )
    if "ix_source_assets_content_fingerprint" not in asset_indexes:
        op.create_index(
            "ix_source_assets_content_fingerprint",
            "source_assets",
            ["content_fingerprint"],
        )

    mention_columns = _columns(bind, "shop_mentions")
    mention_checks = _checks(bind, "shop_mentions")
    mention_foreign_keys = _foreign_keys(bind, "shop_mentions")
    with op.batch_alter_table("shop_mentions") as batch:
        if "reused_from_mention_id" not in mention_columns:
            batch.add_column(sa.Column("reused_from_mention_id", sa.Integer()))
        if "resolution_basis" not in mention_columns:
            batch.add_column(sa.Column("resolution_basis", sa.String(32)))
        if "fk_shop_mentions_reused_from" not in mention_foreign_keys:
            batch.create_foreign_key(
                "fk_shop_mentions_reused_from",
                "shop_mentions",
                ["reused_from_mention_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "ck_mentions_resolution_basis" not in mention_checks:
            batch.create_check_constraint(
                "ck_mentions_resolution_basis",
                "resolution_basis IS NULL OR resolution_basis IN "
                "('source_reuse','existing_shop','verified_candidate',"
                "'verified_collision','image_evidence')",
            )

    mention_indexes = _indexes(bind, "shop_mentions")
    if "ix_shop_mentions_reused_from_mention_id" not in mention_indexes:
        op.create_index(
            "ix_shop_mentions_reused_from_mention_id",
            "shop_mentions",
            ["reused_from_mention_id"],
        )
    if "ix_shop_mentions_resolution_basis" not in mention_indexes:
        op.create_index(
            "ix_shop_mentions_resolution_basis",
            "shop_mentions",
            ["resolution_basis"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    mention_indexes = _indexes(bind, "shop_mentions")
    for index_name in (
        "ix_shop_mentions_resolution_basis",
        "ix_shop_mentions_reused_from_mention_id",
    ):
        if index_name in mention_indexes:
            op.drop_index(index_name, table_name="shop_mentions")
    mention_columns = _columns(bind, "shop_mentions")
    mention_checks = _checks(bind, "shop_mentions")
    mention_foreign_keys = _foreign_keys(bind, "shop_mentions")
    with op.batch_alter_table("shop_mentions") as batch:
        if "ck_mentions_resolution_basis" in mention_checks:
            batch.drop_constraint("ck_mentions_resolution_basis", type_="check")
        if "fk_shop_mentions_reused_from" in mention_foreign_keys:
            batch.drop_constraint("fk_shop_mentions_reused_from", type_="foreignkey")
        if "resolution_basis" in mention_columns:
            batch.drop_column("resolution_basis")
        if "reused_from_mention_id" in mention_columns:
            batch.drop_column("reused_from_mention_id")

    asset_indexes = _indexes(bind, "source_assets")
    for index_name in (
        "ix_source_assets_content_fingerprint",
        "ix_source_assets_normalized_url",
        "ix_source_assets_source_identity",
    ):
        if index_name in asset_indexes:
            op.drop_index(index_name, table_name="source_assets")
    asset_columns = _columns(bind, "source_assets")
    asset_checks = _checks(bind, "source_assets")
    with op.batch_alter_table("source_assets") as batch:
        if "ck_source_assets_identity_pair" in asset_checks:
            batch.drop_constraint("ck_source_assets_identity_pair", type_="check")
        for column_name in (
            "content_fingerprint",
            "normalized_url",
            "source_item_id",
            "source_service",
        ):
            if column_name in asset_columns:
                batch.drop_column(column_name)

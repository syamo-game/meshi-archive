from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260713_01"
down_revision = None
branch_labels = None
depends_on = None


def _create_fresh_schema() -> None:
    from db.models import Base

    Base.metadata.create_all(bind=op.get_bind())


def _create_supporting_tables() -> None:
    op.create_table(
        "source_assets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(20), sa.ForeignKey("messages.message_id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("mime_type", sa.String(255)),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("fetch_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("fetch_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('link','embed','image','attachment')", name="ck_source_assets_kind"),
        sa.CheckConstraint("fetch_status IN ('pending','available','unavailable')", name="ck_source_assets_fetch_status"),
        sa.UniqueConstraint("message_id", "kind", "url", name="uq_source_assets_message_kind_url"),
    )
    op.create_table(
        "shop_mentions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(20), sa.ForeignKey("messages.message_id", ondelete="CASCADE"), nullable=False),
        sa.Column("shop_id", sa.Integer(), sa.ForeignKey("shops.id", ondelete="SET NULL"), nullable=True),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("extracted_name", sa.String(), nullable=False),
        sa.Column("extracted_area", sa.String()),
        sa.Column("extracted_category", sa.String()),
        sa.Column("source_url", sa.Text()),
        sa.Column("resolution_status", sa.String(16), nullable=False, server_default="ambiguous"),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("resolution_method", sa.String(16)),
        sa.Column("difference_type", sa.String(64)),
        sa.Column("extraction_source", sa.String(64), nullable=False, server_default="legacy_import"),
        sa.Column("extraction_error", sa.Text()),
        sa.Column("confidence_reason", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("resolution_status IN ('resolved','ambiguous','not_found','invalid')", name="ck_mentions_resolution_status"),
        sa.CheckConstraint("review_status IN ('pending','approved','rejected','deferred')", name="ck_mentions_review_status"),
        sa.CheckConstraint("resolution_method IS NULL OR resolution_method IN ('automatic','manual')", name="ck_mentions_resolution_method"),
        sa.UniqueConstraint("message_id", "occurrence_index", name="uq_mentions_message_occurrence"),
    )
    op.create_index("ix_shop_mentions_shop_id", "shop_mentions", ["shop_id"])
    op.create_index("ix_shop_mentions_resolution_status", "shop_mentions", ["resolution_status"])
    op.create_index("ix_shop_mentions_review_status", "shop_mentions", ["review_status"])
    op.create_index("ix_shop_mentions_difference_type", "shop_mentions", ["difference_type"])
    op.create_table(
        "resolution_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mention_id", sa.Integer(), sa.ForeignKey("shop_mentions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("area", sa.String()),
        sa.Column("category", sa.String()),
        sa.Column("address", sa.Text()),
        sa.Column("phone", sa.String(32)),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("external_source", sa.String(64)),
        sa.Column("external_id", sa.String(255)),
        sa.Column("evidence_url", sa.Text()),
        sa.Column("matched_fields", sa.Text()),
        sa.Column("conflicting_fields", sa.Text()),
        sa.Column("name_similarity_milli", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_strong_match", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("name_similarity_milli BETWEEN 0 AND 1000", name="ck_candidates_similarity"),
        sa.UniqueConstraint("mention_id", "rank", name="uq_candidates_mention_rank"),
    )
    op.create_table(
        "sync_states",
        sa.Column("channel_id", sa.String(20), primary_key=True),
        sa.Column("last_contiguous_message_id", sa.String(20)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(20), sa.ForeignKey("messages.message_id", ondelete="SET NULL")),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("web_search_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("image_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_microusd", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','processing','succeeded','failed','ignored')", name="ck_processing_runs_status"),
    )
    op.create_table(
        "review_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mention_id", sa.Integer(), sa.ForeignKey("shop_mentions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("previous_shop_id", sa.Integer()),
        sa.Column("selected_shop_id", sa.Integer()),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "lookup_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lookup_cache_kind", "lookup_cache", ["kind"])
    op.create_index("ix_lookup_cache_expires_at", "lookup_cache", ["expires_at"])
    op.create_table(
        "import_batches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="validated"),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('validated','applied','failed')", name="ck_import_batches_status"),
    )
    op.create_table(
        "import_rows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(32), sa.ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shop_name", sa.String(), nullable=False),
        sa.Column("area", sa.String()),
        sa.Column("category", sa.String()),
        sa.Column("address", sa.Text()),
        sa.Column("phone", sa.String(32)),
        sa.Column("external_source", sa.String(64)),
        sa.Column("external_id", sa.String(255)),
        sa.Column("is_visited", sa.Boolean(), nullable=False),
        sa.Column("visited_at", sa.DateTime(timezone=True)),
        sa.Column("rating", sa.Integer()),
        sa.Column("memo", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("extraction_source", sa.String(64), nullable=False),
        sa.Column("extraction_error", sa.Text()),
        sa.Column("confidence_reason", sa.Text()),
        sa.CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_import_rows_rating"),
        sa.UniqueConstraint("batch_id", "row_number", name="uq_import_rows_batch_row"),
        sa.UniqueConstraint("batch_id", "shop_id", name="uq_import_rows_batch_shop"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "messages" not in inspector.get_table_names():
        _create_fresh_schema()
        return

    shop_columns = {column["name"] for column in inspector.get_columns("shops")}
    legacy_columns = {
        "visited_at": sa.Column("visited_at", sa.DateTime(timezone=True)),
        "rating": sa.Column("rating", sa.Integer()),
        "memo": sa.Column("memo", sa.Text()),
        "needs_review": sa.Column(
            "needs_review", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        "extraction_source": sa.Column("extraction_source", sa.String()),
        "extraction_error": sa.Column("extraction_error", sa.Text()),
        "confidence_reason": sa.Column("confidence_reason", sa.Text()),
    }
    missing_legacy_columns = [
        column for name, column in legacy_columns.items() if name not in shop_columns
    ]
    if missing_legacy_columns:
        with op.batch_alter_table("shops") as batch:
            for column in missing_legacy_columns:
                batch.add_column(column)

    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("channel_id", sa.String(20)))
        batch.add_column(sa.Column("content", sa.Text()))
        batch.add_column(sa.Column("source_created_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("processing_status", sa.String(16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("fetch_error", sa.Text()))
        batch.add_column(sa.Column("processed_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_messages_channel_id", ["channel_id"])
        batch.create_index("ix_messages_processing_status", ["processing_status"])
        batch.create_check_constraint("ck_messages_id_length", "length(message_id) BETWEEN 17 AND 20")
        batch.create_check_constraint(
            "ck_messages_processing_status",
            "processing_status IN ('pending','processing','succeeded','failed','ignored')",
        )

    with op.batch_alter_table("shops") as batch:
        batch.add_column(sa.Column("address", sa.Text()))
        batch.add_column(sa.Column("phone", sa.String(32)))
        batch.add_column(sa.Column("canonical_url", sa.Text()))
        batch.add_column(sa.Column("external_source", sa.String(64)))
        batch.add_column(sa.Column("external_id", sa.String(255)))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.create_index("ix_shops_phone", ["phone"])
        batch.create_unique_constraint("uq_shops_external_identity", ["external_source", "external_id"])
        batch.create_check_constraint("ck_shops_rating", "rating IS NULL OR rating BETWEEN 1 AND 5")
        batch.create_check_constraint(
            "ck_shops_external_identity_pair",
            "(external_source IS NULL AND external_id IS NULL) OR "
            "(external_source IS NOT NULL AND external_id IS NOT NULL)",
        )

    _create_supporting_tables()

    op.execute(sa.text("UPDATE messages SET processing_status='succeeded', processed_at=CURRENT_TIMESTAMP"))
    op.execute(sa.text("UPDATE shops SET updated_at=created_at WHERE updated_at IS NULL"))
    op.execute(
        sa.text(
            "UPDATE messages SET source_created_at=(SELECT MIN(shops.created_at) FROM shops "
            "WHERE shops.message_id=messages.message_id)"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO shop_mentions "
            "(message_id, shop_id, occurrence_index, extracted_name, extracted_area, "
            "extracted_category, source_url, resolution_status, review_status, "
            "resolution_method, difference_type, extraction_source, extraction_error, "
            "confidence_reason, reviewed_at, version, created_at) "
            "SELECT message_id, id, ROW_NUMBER() OVER (PARTITION BY message_id ORDER BY id)-1, "
            "shop_name, area, category, url, "
            "CASE WHEN needs_review THEN 'ambiguous' ELSE 'resolved' END, "
            "CASE WHEN needs_review THEN 'pending' ELSE 'approved' END, "
            "CASE WHEN needs_review THEN NULL ELSE 'manual' END, "
            "CASE WHEN needs_review THEN 'legacy_review' ELSE NULL END, "
            "COALESCE(extraction_source, 'legacy_import'), extraction_error, confidence_reason, "
            "CASE WHEN needs_review THEN NULL ELSE created_at END, 1, created_at FROM shops"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO source_assets (message_id, kind, url, fetch_status, created_at) "
            "SELECT DISTINCT message_id, 'link', url, 'pending', CURRENT_TIMESTAMP "
            "FROM shops WHERE url IS NOT NULL AND url <> ''"
        )
    )

    with op.batch_alter_table("shops") as batch:
        batch.drop_column("message_id")
        batch.drop_column("url")
        batch.drop_column("needs_review")
        batch.drop_column("extraction_source")
        batch.drop_column("extraction_error")
        batch.drop_column("confidence_reason")
        batch.alter_column("updated_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.add_column(sa.Column("message_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("url", sa.Text()))
        batch.add_column(sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("extraction_source", sa.String()))
        batch.add_column(sa.Column("extraction_error", sa.Text()))
        batch.add_column(sa.Column("confidence_reason", sa.Text()))

    op.execute(
        sa.text(
            "UPDATE shops SET message_id=(SELECT message_id FROM shop_mentions WHERE shop_id=shops.id ORDER BY id LIMIT 1), "
            "url=(SELECT source_url FROM shop_mentions WHERE shop_id=shops.id ORDER BY id LIMIT 1), "
            "needs_review=COALESCE((SELECT CASE WHEN review_status='pending' THEN TRUE ELSE FALSE END FROM shop_mentions WHERE shop_id=shops.id ORDER BY id LIMIT 1), FALSE), "
            "extraction_source=(SELECT extraction_source FROM shop_mentions WHERE shop_id=shops.id ORDER BY id LIMIT 1), "
            "extraction_error=(SELECT extraction_error FROM shop_mentions WHERE shop_id=shops.id ORDER BY id LIMIT 1), "
            "confidence_reason=(SELECT confidence_reason FROM shop_mentions WHERE shop_id=shops.id ORDER BY id LIMIT 1)"
        )
    )

    for table in (
        "import_rows",
        "import_batches",
        "lookup_cache",
        "review_events",
        "processing_runs",
        "sync_states",
        "resolution_candidates",
        "source_assets",
        "shop_mentions",
    ):
        op.drop_table(table)

    with op.batch_alter_table("shops") as batch:
        batch.drop_constraint("ck_shops_rating", type_="check")
        batch.drop_constraint("ck_shops_external_identity_pair", type_="check")
        batch.drop_constraint("uq_shops_external_identity", type_="unique")
        batch.drop_index("ix_shops_phone")
        for column in (
            "address",
            "phone",
            "canonical_url",
            "external_source",
            "external_id",
            "updated_at",
            "version",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("ck_messages_processing_status", type_="check")
        batch.drop_constraint("ck_messages_id_length", type_="check")
        batch.drop_index("ix_messages_processing_status")
        batch.drop_index("ix_messages_channel_id")
        for column in (
            "channel_id",
            "content",
            "source_created_at",
            "processing_status",
            "fetch_error",
            "processed_at",
        ):
            batch.drop_column(column)

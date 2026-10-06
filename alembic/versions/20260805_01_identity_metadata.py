from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_01"
down_revision = "20260713_01"
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


def upgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())

    if "branch_name" not in _columns(bind, "shops"):
        with op.batch_alter_table("shops") as batch:
            batch.add_column(sa.Column("branch_name", sa.String(255)))

    mention_columns = _columns(bind, "shop_mentions")
    mention_checks = _checks(bind, "shop_mentions")
    with op.batch_alter_table("shop_mentions") as batch:
        if "extracted_branch_name" not in mention_columns:
            batch.add_column(sa.Column("extracted_branch_name", sa.String(255)))
        if "metadata_review_status" not in mention_columns:
            batch.add_column(
                sa.Column(
                    "metadata_review_status",
                    sa.String(16),
                    nullable=False,
                    server_default="approved",
                )
            )
        if "metadata_difference_type" not in mention_columns:
            batch.add_column(sa.Column("metadata_difference_type", sa.String(64)))
        if "metadata_reviewed_at" not in mention_columns:
            batch.add_column(sa.Column("metadata_reviewed_at", sa.DateTime(timezone=True)))
        if "ck_mentions_metadata_review_status" not in mention_checks:
            batch.create_check_constraint(
                "ck_mentions_metadata_review_status",
                "metadata_review_status IN ('pending','approved','deferred')",
            )

    mention_indexes = _indexes(bind, "shop_mentions")
    if "ix_shop_mentions_metadata_review_status" not in mention_indexes:
        op.create_index(
            "ix_shop_mentions_metadata_review_status",
            "shop_mentions",
            ["metadata_review_status"],
        )
    if "ix_shop_mentions_metadata_difference_type" not in mention_indexes:
        op.create_index(
            "ix_shop_mentions_metadata_difference_type",
            "shop_mentions",
            ["metadata_difference_type"],
        )

    candidate_columns = _columns(bind, "resolution_candidates")
    candidate_checks = _checks(bind, "resolution_candidates")
    with op.batch_alter_table("resolution_candidates") as batch:
        if "provenance" not in candidate_columns:
            batch.add_column(
                sa.Column(
                    "provenance",
                    sa.String(32),
                    nullable=False,
                    server_default="web_search",
                )
            )
        if "is_verified" not in candidate_columns:
            batch.add_column(
                sa.Column(
                    "is_verified",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "verification_reason" not in candidate_columns:
            batch.add_column(sa.Column("verification_reason", sa.Text()))
        if "ck_candidates_provenance" not in candidate_checks:
            batch.create_check_constraint(
                "ck_candidates_provenance",
                "provenance IN ('posted_url','structured_data','web_search','image')",
            )

    event_columns = _columns(bind, "review_events")
    event_checks = _checks(bind, "review_events")
    with op.batch_alter_table("review_events") as batch:
        if "scope" not in event_columns:
            batch.add_column(
                sa.Column(
                    "scope",
                    sa.String(16),
                    nullable=False,
                    server_default="identity",
                )
            )
        if "ck_review_events_scope" not in event_checks:
            batch.create_check_constraint(
                "ck_review_events_scope",
                "scope IN ('identity','metadata')",
            )

    if "shop_redirects" not in table_names:
        op.create_table(
            "shop_redirects",
            sa.Column("source_shop_id", sa.Integer(), primary_key=True),
            sa.Column(
                "target_shop_id",
                sa.Integer(),
                sa.ForeignKey("shops.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("reason", sa.String(64), nullable=False, server_default="merge"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "source_shop_id <> target_shop_id",
                name="ck_shop_redirects_distinct",
            ),
        )
        op.create_index(
            "ix_shop_redirects_target_shop_id",
            "shop_redirects",
            ["target_shop_id"],
        )

    import_row_columns = _columns(bind, "import_rows")
    import_row_checks = _checks(bind, "import_rows")
    with op.batch_alter_table("import_rows") as batch:
        if "branch_name" not in import_row_columns:
            batch.add_column(sa.Column("branch_name", sa.String(255)))
        if "resolution_status" not in import_row_columns:
            batch.add_column(
                sa.Column(
                    "resolution_status",
                    sa.String(16),
                    nullable=False,
                    server_default="resolved",
                )
            )
        if "review_status" not in import_row_columns:
            batch.add_column(
                sa.Column(
                    "review_status",
                    sa.String(16),
                    nullable=False,
                    server_default="approved",
                )
            )
        if "metadata_review_status" not in import_row_columns:
            batch.add_column(
                sa.Column(
                    "metadata_review_status",
                    sa.String(16),
                    nullable=False,
                    server_default="approved",
                )
            )
        if "resolution_method" not in import_row_columns:
            batch.add_column(sa.Column("resolution_method", sa.String(16)))
        if "difference_type" not in import_row_columns:
            batch.add_column(sa.Column("difference_type", sa.String(64)))
        if "metadata_difference_type" not in import_row_columns:
            batch.add_column(sa.Column("metadata_difference_type", sa.String(64)))
        if "reviewed_at" not in import_row_columns:
            batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True)))
        if "metadata_reviewed_at" not in import_row_columns:
            batch.add_column(sa.Column("metadata_reviewed_at", sa.DateTime(timezone=True)))
        if "ck_import_rows_resolution_status" not in import_row_checks:
            batch.create_check_constraint(
                "ck_import_rows_resolution_status",
                "resolution_status IN ('resolved','ambiguous','not_found','invalid')",
            )
        if "ck_import_rows_review_status" not in import_row_checks:
            batch.create_check_constraint(
                "ck_import_rows_review_status",
                "review_status IN ('pending','approved','rejected','deferred')",
            )
        if "ck_import_rows_metadata_review_status" not in import_row_checks:
            batch.create_check_constraint(
                "ck_import_rows_metadata_review_status",
                "metadata_review_status IN ('pending','approved','deferred')",
            )
        if "ck_import_rows_resolution_method" not in import_row_checks:
            batch.create_check_constraint(
                "ck_import_rows_resolution_method",
                "resolution_method IS NULL OR resolution_method IN ('automatic','manual')",
            )

    op.execute(
        sa.text(
            "UPDATE import_rows SET "
            "resolution_status=CASE WHEN needs_review THEN 'ambiguous' ELSE 'resolved' END, "
            "review_status=CASE WHEN needs_review THEN 'pending' ELSE 'approved' END, "
            "metadata_review_status='approved', "
            "resolution_method=CASE WHEN needs_review THEN NULL ELSE 'manual' END, "
            "difference_type=CASE WHEN needs_review THEN 'legacy_review' ELSE NULL END, "
            "metadata_difference_type=NULL, "
            "reviewed_at=CASE WHEN needs_review THEN NULL ELSE created_at END, "
            "metadata_reviewed_at=created_at"
        )
    )

    op.execute(
        sa.text(
            "UPDATE shop_mentions SET "
            "metadata_review_status='pending', "
            "metadata_difference_type='unknown_category' "
            "WHERE difference_type='unknown_category'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "shop_redirects" in table_names:
        redirect_count = bind.execute(sa.text("SELECT COUNT(*) FROM shop_redirects")).scalar_one()
        if redirect_count:
            raise RuntimeError(
                "Cannot downgrade while shop redirects exist; restore merged shops first."
            )
        op.drop_table("shop_redirects")

    import_row_columns = _columns(bind, "import_rows")
    import_row_checks = _checks(bind, "import_rows")
    with op.batch_alter_table("import_rows") as batch:
        for constraint_name in (
            "ck_import_rows_resolution_method",
            "ck_import_rows_metadata_review_status",
            "ck_import_rows_review_status",
            "ck_import_rows_resolution_status",
        ):
            if constraint_name in import_row_checks:
                batch.drop_constraint(constraint_name, type_="check")
        for column_name in (
            "metadata_reviewed_at",
            "reviewed_at",
            "metadata_difference_type",
            "difference_type",
            "resolution_method",
            "metadata_review_status",
            "review_status",
            "resolution_status",
            "branch_name",
        ):
            if column_name in import_row_columns:
                batch.drop_column(column_name)

    event_columns = _columns(bind, "review_events")
    event_checks = _checks(bind, "review_events")
    with op.batch_alter_table("review_events") as batch:
        if "ck_review_events_scope" in event_checks:
            batch.drop_constraint("ck_review_events_scope", type_="check")
        if "scope" in event_columns:
            batch.drop_column("scope")

    candidate_columns = _columns(bind, "resolution_candidates")
    candidate_checks = _checks(bind, "resolution_candidates")
    with op.batch_alter_table("resolution_candidates") as batch:
        if "ck_candidates_provenance" in candidate_checks:
            batch.drop_constraint("ck_candidates_provenance", type_="check")
        for column_name in ("verification_reason", "is_verified", "provenance"):
            if column_name in candidate_columns:
                batch.drop_column(column_name)

    mention_indexes = _indexes(bind, "shop_mentions")
    if "ix_shop_mentions_metadata_difference_type" in mention_indexes:
        op.drop_index(
            "ix_shop_mentions_metadata_difference_type",
            table_name="shop_mentions",
        )
    if "ix_shop_mentions_metadata_review_status" in mention_indexes:
        op.drop_index(
            "ix_shop_mentions_metadata_review_status",
            table_name="shop_mentions",
        )
    mention_columns = _columns(bind, "shop_mentions")
    mention_checks = _checks(bind, "shop_mentions")
    with op.batch_alter_table("shop_mentions") as batch:
        if "ck_mentions_metadata_review_status" in mention_checks:
            batch.drop_constraint("ck_mentions_metadata_review_status", type_="check")
        for column_name in (
            "metadata_reviewed_at",
            "metadata_difference_type",
            "metadata_review_status",
            "extracted_branch_name",
        ):
            if column_name in mention_columns:
                batch.drop_column(column_name)

    if "branch_name" in _columns(bind, "shops"):
        with op.batch_alter_table("shops") as batch:
            batch.drop_column("branch_name")

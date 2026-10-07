from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from db.models import Shop, ShopMention


class MergedShopSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    id: int
    shop_name: str
    branch_name: str | None
    area: str | None
    category: str | None
    address: str | None
    phone: str | None
    canonical_url: str | None
    image_key: str | None
    external_source: str | None
    external_id: str | None
    is_visited: bool
    visited_at: datetime | None
    rating: int | None
    memo: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class MovedMentionSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    id: int
    message_id: str
    occurrence_index: int
    shop_id: int | None
    version: int
    review_status: str
    metadata_review_status: str
    resolution_status: str
    resolution_method: str | None
    reviewed_at: datetime | None
    metadata_reviewed_at: datetime | None


class MergeHistorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["shop_merge_snapshot"] = "shop_merge_snapshot"
    reason: str
    shops: tuple[MergedShopSnapshot, ...]
    moved_mentions: tuple[MovedMentionSnapshot, ...]


def snapshot_merge(
    shops: tuple[Shop, ...],
    mentions: tuple[ShopMention, ...],
    *,
    reason: str,
) -> str:
    return MergeHistorySnapshot(
        reason=reason,
        shops=tuple(MergedShopSnapshot.model_validate(shop) for shop in shops),
        moved_mentions=tuple(
            MovedMentionSnapshot.model_validate(mention) for mention in mentions
        ),
    ).model_dump_json()

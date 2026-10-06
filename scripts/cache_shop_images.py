from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

sys.path.append(str(Path(__file__).resolve().parents[1]))
load_dotenv()

from db.database import SessionLocal
from db.models import (
    AssetKind,
    FetchStatus,
    MetadataReviewStatus,
    ReviewStatus,
    ShopMention,
    SourceAsset,
)
from services.shop_image_cache import cache_shop_image, existing_processed_image_url


@dataclass(frozen=True)
class CacheArgs:
    apply: bool
    force: bool
    limit: int | None
    message_ids: tuple[str, ...]


@dataclass
class CacheCounts:
    candidates: int = 0
    created: int = 0
    already_cached: int = 0
    failed: int = 0


def parse_args() -> CacheArgs:
    parser = argparse.ArgumentParser(
        description="Download approved source images and crop them to the card aspect ratio."
    )
    parser.add_argument("--apply", action="store_true", help="Write processed image files.")
    parser.add_argument("--force", action="store_true", help="Rebuild files that already exist.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--message-id",
        action="append",
        default=[],
        help="Process only this message ID. Repeat to select multiple messages.",
    )
    namespace = parser.parse_args()
    message_ids = tuple(dict.fromkeys(namespace.message_id))
    if namespace.limit is not None and namespace.limit < 1:
        parser.error("--limit must be at least 1")
    if namespace.limit is not None and message_ids:
        parser.error("--limit and --message-id cannot be used together")
    invalid_ids = [
        message_id
        for message_id in message_ids
        if not message_id.isdigit() or not 17 <= len(message_id) <= 20
    ]
    if invalid_ids:
        parser.error("--message-id must be an exact 17-20 digit string")
    return CacheArgs(
        apply=namespace.apply,
        force=namespace.force,
        limit=namespace.limit,
        message_ids=message_ids,
    )


async def run(args: CacheArgs) -> CacheCounts:
    db = SessionLocal()
    counts = CacheCounts()
    failures: list[str] = []
    try:
        query = (
            db.query(SourceAsset)
            .join(ShopMention, ShopMention.message_id == SourceAsset.message_id)
            .filter(
                SourceAsset.kind == AssetKind.IMAGE.value,
                SourceAsset.fetch_status != FetchStatus.UNAVAILABLE.value,
                ShopMention.review_status == ReviewStatus.APPROVED.value,
                ShopMention.metadata_review_status == MetadataReviewStatus.APPROVED.value,
            )
            .order_by(SourceAsset.id)
            .distinct()
        )
        if args.message_ids:
            query = query.filter(SourceAsset.message_id.in_(args.message_ids))
        elif args.limit is not None:
            query = query.limit(args.limit)
        assets = query.all()
        counts.candidates = len(assets)
        if not args.apply:
            counts.already_cached = sum(
                existing_processed_image_url(asset.url) is not None for asset in assets
            )
            return counts

        for asset in assets:
            try:
                result = await cache_shop_image(asset.url, force=args.force)
            except Exception as exc:
                counts.failed += 1
                failures.append(
                    f"asset_id={asset.id} message_id={asset.message_id} "
                    f"url={asset.url!r} error={exc}"
                )
                continue
            if result.already_cached:
                counts.already_cached += 1
            else:
                counts.created += 1
    finally:
        db.close()

    if failures:
        raise RuntimeError("Image processing failed:\n" + "\n".join(failures))
    return counts


async def async_main() -> None:
    args = parse_args()
    counts = await run(args)
    payload = {"status": "applied" if args.apply else "dry_run", **asdict(counts)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())

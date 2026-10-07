from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session, selectinload

from db.models import Message, Shop, ShopMention
from services.extraction_safety import is_event_excluded_registration


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SIGNED_QUERY = re.compile(
    r"(?:^|[&;])(?:x-amz-[^=]+|x-goog-[^=]+|sig|signature|token|access_token|auth|authorization|password|key|ex|is|hm)=",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewAuditCsv:
    content: bytes
    row_count: int
    message_count: int
    unlinked_count: int


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _cell(value: str | None, attachment_urls: set[str]) -> str:
    def redact(match: re.Match[str]) -> str:
        original = match.group()
        url = original.rstrip(".,;)]}")
        base, separator, query = url.partition("?")
        if separator and (base.lower() in attachment_urls or _SIGNED_QUERY.search(query)):
            return base + "?__redacted__" + original[len(url):]
        return original

    text = _URL.sub(redact, value or "")
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def export_review_audit(db: Session) -> ReviewAuditCsv:
    mentions = (
        db.query(ShopMention)
        .options(
            selectinload(ShopMention.message).selectinload(Message.assets),
            selectinload(ShopMention.shop).selectinload(Shop.mentions),
            selectinload(ShopMention.review_events),
        )
        .order_by(ShopMention.id.asc())
        .all()
    )
    attachment_urls = {
        asset.url.partition("?")[0].lower()
        for mention in mentions
        for asset in mention.message.assets
        if asset.kind in {"attachment", "image"}
    }
    for mention in mentions:
        for line in (mention.message.content or "").splitlines():
            if line.startswith("[Attachment]"):
                attachment_urls.update(
                    match.group().rstrip(".,;)]}").partition("?")[0].lower()
                    for match in _URL.finditer(line)
                )

    def cell(value: str | None) -> str:
        return _cell(value, attachment_urls)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "mention_id", "mention.version", "message_id", "occurrence_index",
        "shop.id", "shop.version", "shop.name", "shop.branch_name", "shop.area", "shop.category",
        "shop.is_public", "is_representative_mention",
        "extracted_name", "extracted_branch_name", "extracted_area", "extracted_category",
        "review_status", "metadata_review_status", "resolution_status", "needs_review",
        "difference_type", "metadata_difference_type", "resolution_method", "resolution_basis",
        "reused_from_mention_id", "source_url", "extraction_source", "extraction_error", "confidence_reason",
        "reviewed_at", "metadata_reviewed_at", "message.source_created_at", "message.content",
        "message.processing_status", "message.fetch_error", "message.asset_count", "mention.review_event_count",
    ])
    for mention in mentions:
        shop = mention.shop
        message = mention.message
        public_shop = bool(shop and any(
            item.review_status == "approved" and item.metadata_review_status == "approved"
            for item in shop.mentions
        ))
        representative = shop.primary_mention() if shop else None
        writer.writerow([
            mention.id, mention.version, mention.message_id, mention.occurrence_index,
            shop.id if shop else "", shop.version if shop else "",
            cell(shop.shop_name if shop else None), cell(shop.branch_name if shop else None),
            cell(shop.area if shop else None), cell(shop.category if shop else None),
            public_shop, representative is not None and representative.id == mention.id,
            cell(mention.extracted_name), cell(mention.extracted_branch_name),
            cell(mention.extracted_area), cell(mention.extracted_category),
            mention.review_status, mention.metadata_review_status, mention.resolution_status,
            not is_event_excluded_registration(mention)
            and not (mention.review_status == "rejected" and mention.difference_type == "manual_excluded") and (
                mention.review_status in {"pending", "deferred"}
                or mention.metadata_review_status in {"pending", "deferred"}
            ),
            cell(mention.difference_type), cell(mention.metadata_difference_type),
            cell(mention.resolution_method), cell(mention.resolution_basis),
            mention.reused_from_mention_id or "", cell(mention.source_url),
            cell(mention.extraction_source), cell(mention.extraction_error), cell(mention.confidence_reason),
            _as_utc(mention.reviewed_at).isoformat() if mention.reviewed_at else "",
            _as_utc(mention.metadata_reviewed_at).isoformat() if mention.metadata_reviewed_at else "",
            _as_utc(message.source_created_at).isoformat() if message.source_created_at else "",
            cell(message.content), message.processing_status, cell(message.fetch_error),
            len(message.assets), len(mention.review_events),
        ])
    return ReviewAuditCsv(
        content=output.getvalue().encode("utf-8-sig"),
        row_count=len(mentions),
        message_count=len({mention.message_id for mention in mentions}),
        unlinked_count=sum(mention.shop_id is None for mention in mentions),
    )

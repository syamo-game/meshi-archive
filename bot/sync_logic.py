from __future__ import annotations

import logging
import os
import re
from datetime import timezone
from urllib.parse import urlparse

import discord
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Message, ProcessingStatus, Shop, SyncState, utc_now
from services.identification_pipeline import (
    MessageEnvelope,
    SourceAssetInput,
    process_message,
)


logger = logging.getLogger(__name__)
SYNC_BATCH_LIMIT = max(1, int(os.getenv("SYNC_BATCH_LIMIT", "500")))


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def fill_missing_shop_fields(
    shop: Shop,
    shop_info: dict[str, str | bool | None],
    url_fallback: str | None = None,
) -> bool:
    updates = {
        "area": _clean_optional(shop_info.get("area")),
        "category": _clean_optional(shop_info.get("category")),
        "canonical_url": _clean_optional(shop_info.get("url")) or _clean_optional(url_fallback),
    }
    changed = False
    for attr, value in updates.items():
        if not getattr(shop, attr) and value:
            setattr(shop, attr, value)
            changed = True
    return changed


async def find_duplicate_shop(
    db: Session,
    shop_info: dict[str, str | bool | None],
) -> Shop | None:
    url = _clean_optional(shop_info.get("url"))
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    return db.query(Shop).filter(Shop.canonical_url == url).first()


def _extract_url(text: str) -> str | None:
    urls = re.findall(r"https?://[^\s<>]+", text)
    return urls[0].rstrip(".,);]」』") if urls else None


def _build_text_to_parse(message: discord.Message) -> str:
    parts = [message.content.strip()] if message.content.strip() else []
    for embed in message.embeds:
        if embed.url:
            parts.append(f"[Embed URL] {embed.url}")
        if embed.title:
            parts.append(f"[Embed Title] {embed.title}")
        if embed.description:
            parts.append(f"[Embed Description] {embed.description}")
        for field in embed.fields:
            parts.append(f"[Embed Field: {field.name}] {field.value}")
    for attachment in message.attachments:
        parts.append(
            f"[Attachment] filename={attachment.filename} "
            f"content_type={attachment.content_type or 'unknown'} url={attachment.url}"
        )
    return "\n".join(parts) or "[Empty Discord message]"


def _append_asset(
    assets: list[SourceAssetInput],
    seen: set[tuple[str, str]],
    asset: SourceAssetInput,
) -> None:
    key = (asset.kind, asset.url)
    if key in seen:
        if not asset.is_embed_preview:
            for index, existing in enumerate(assets):
                if (existing.kind, existing.url) == key and existing.is_embed_preview:
                    assets[index] = asset
                    break
        return
    seen.add(key)
    assets.append(asset)


def build_message_envelope(message: discord.Message) -> MessageEnvelope:
    assets: list[SourceAssetInput] = []
    seen: set[tuple[str, str]] = set()

    for url in re.findall(r"https?://[^\s<>]+", message.content):
        _append_asset(
            assets,
            seen,
            SourceAssetInput(kind="link", url=url.rstrip(".,);]」』")),
        )

    for embed in message.embeds:
        if embed.url:
            _append_asset(
                assets,
                seen,
                SourceAssetInput(
                    kind="embed",
                    url=embed.url,
                    title=embed.title,
                    description=embed.description,
                ),
            )
        if embed.image and embed.image.url:
            _append_asset(
                assets,
                seen,
                SourceAssetInput(
                    kind="image",
                    url=embed.image.url,
                    is_embed_preview=bool(embed.url),
                ),
            )
        if embed.thumbnail and embed.thumbnail.url:
            _append_asset(
                assets,
                seen,
                SourceAssetInput(
                    kind="image",
                    url=embed.thumbnail.url,
                    is_embed_preview=bool(embed.url),
                ),
            )

    for attachment in message.attachments:
        content_type = attachment.content_type or "application/octet-stream"
        kind = "image" if content_type.lower().startswith("image/") else "attachment"
        _append_asset(
            assets,
            seen,
            SourceAssetInput(
                kind=kind,
                url=attachment.url,
                title=attachment.filename,
                mime_type=content_type,
            ),
        )

    created_at = message.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return MessageEnvelope(
        message_id=str(message.id),
        channel_id=str(message.channel.id),
        content=_build_text_to_parse(message),
        created_at=created_at,
        assets=tuple(assets[:50]),
    )


async def sync_history(client: discord.Client, command_message: discord.Message) -> None:
    channel = command_message.channel
    await channel.send("過去メッセージの同期を開始します。")
    db = SessionLocal()
    try:
        channel_id = str(channel.id)
        state = db.query(SyncState).filter(SyncState.channel_id == channel_id).first()
        if state is None:
            state = SyncState(channel_id=channel_id)
            db.add(state)
            db.commit()

        after = (
            discord.Object(id=int(state.last_contiguous_message_id))
            if state.last_contiguous_message_id
            else None
        )
        processed = 0
        registered = 0
        pending = 0

        async for history_message in channel.history(
            limit=SYNC_BATCH_LIMIT,
            after=after,
            oldest_first=True,
        ):
            if history_message.author == client.user:
                state.last_contiguous_message_id = str(history_message.id)
                state.updated_at = utc_now()
                db.commit()
                continue
            if not (
                history_message.content.strip()
                or history_message.embeds
                or history_message.attachments
            ):
                state.last_contiguous_message_id = str(history_message.id)
                state.updated_at = utc_now()
                db.commit()
                continue

            existing = (
                db.query(Message)
                .filter(Message.message_id == str(history_message.id))
                .first()
            )
            if existing and existing.processing_status in {
                ProcessingStatus.SUCCEEDED.value,
                ProcessingStatus.IGNORED.value,
            }:
                state.last_contiguous_message_id = str(history_message.id)
                state.updated_at = utc_now()
                db.commit()
                continue

            try:
                result = await process_message(
                    db,
                    build_message_envelope(history_message),
                    allow_source_discovery=True,
                )
            except Exception as exc:
                logger.exception(
                    "History sync stopped: channel_id=%s message_id=%s error=%s",
                    channel_id,
                    history_message.id,
                    exc,
                )
                await channel.send(
                    "同期を停止しました。"
                    f"message_id={history_message.id} の処理に失敗しました。再実行すると同じ投稿から再開します。"
                )
                return

            processed += 1
            registered += len(result.shop_ids)
            pending += len(result.pending_mention_ids)
            state = db.query(SyncState).filter(SyncState.channel_id == channel_id).one()
            state.last_contiguous_message_id = str(history_message.id)
            state.updated_at = utc_now()
            db.commit()

        await channel.send(
            f"同期が完了しました。処理={processed}件、店舗候補={registered}件、データ確認={pending}件。"
        )
    except Exception as exc:
        db.rollback()
        logger.exception("History sync failed: channel_id=%s error=%s", channel.id, exc)
        await channel.send(f"同期処理に失敗しました。channel_id={channel.id}")
    finally:
        db.close()

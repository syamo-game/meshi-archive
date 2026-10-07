from __future__ import annotations

import logging
from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from db.database import SessionLocal
from services.review_suggestions import (
    ReviewSuggestionRequest,
    ReviewSuggestionResponse,
    SuggestionFailure,
    load_suggestion_context,
    reserve_suggestion,
    suggest_shops,
    verify_suggestion_context,
)
from web.auth import is_admin
from web.csrf import verify_csrf_token
from web.read_only import require_writable


logger = logging.getLogger(__name__)
router = APIRouter()


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db


@router.post(
    "/api/admin/reviews/{mention_id}/suggestions",
    response_model=ReviewSuggestionResponse,
)
async def review_suggestions(
    mention_id: int,
    body: ReviewSuggestionRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ReviewSuggestionResponse:
    response.headers["Cache-Control"] = "private, no-store"
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    require_writable()
    verify_csrf_token(request)
    try:
        with reserve_suggestion(mention_id):
            context = await run_in_threadpool(load_suggestion_context, db, mention_id, body)
            result = await suggest_shops(context, body)
            await run_in_threadpool(verify_suggestion_context, db, context, body)
            return result
    except SuggestionFailure as exc:
        headers: dict[str, str] = {"Cache-Control": "private, no-store"}
        if exc.status == 429:
            headers["Retry-After"] = "2"
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": str(exc)},
            headers=headers,
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Suggestion storage read failed: mention_id=%s error_type=%s", mention_id, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"code": "suggestion_storage_unavailable", "message": "保存データを確認できませんでした。時間をおいて再度お試しください。"},
            headers={"Cache-Control": "private, no-store"},
        ) from exc

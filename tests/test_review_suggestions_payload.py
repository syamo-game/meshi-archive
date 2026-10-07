from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from bot import restaurant_extractor as extractor


class SentPayload(BaseModel):
    input: str
    store: bool
    max_tool_calls: int


def test_supplement_and_reference_are_sent_as_separate_search_clues() -> None:
    captured: list[SentPayload] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(SentPayload.model_validate_json(request.content))
        return httpx.Response(400, json={"error": {"message": "synthetic stop", "type": "invalid_request_error"}})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
            client = AsyncOpenAI(api_key="synthetic", base_url="https://model-test.invalid/v1", http_client=transport, max_retries=0)
            mention = extractor.ExtractedMention(shop_name="検索対象", branch_name="銀座店", area="銀座", needs_review=True, confidence_reason="管理者入力")
            hints = extractor.CandidateSearchContext(address="中央区1-2-3", phone="0312345678", category="寿司", supplement="2階の赤い看板", reference_urls=("https://example.com/one",))
            with patch.object(extractor, "_get_client", return_value=client):
                with pytest.raises(extractor.ExtractionError, match="synthetic stop"):
                    await extractor.search_restaurant_candidates(mention, context=hints)

    asyncio.run(exercise())
    assert len(captured) == 1
    payload = captured[0]
    assert payload.input.startswith("店舗名: 検索対象\n支店名: 銀座店\nエリア: 銀座\n")
    assert "未検証の検索手掛かり" in payload.input
    assert "中央区1-2-3" in payload.input
    assert "0312345678" in payload.input
    assert "2階の赤い看板" in payload.input
    assert "https://example.com/one" in payload.input
    assert payload.store is False
    assert payload.max_tool_calls == 1


def test_deadline_cancels_real_sdk_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import review_suggestions as suggestions

    stopped: list[bool] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")
        finally:
            stopped.append(True)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
            client = AsyncOpenAI(api_key="synthetic", base_url="https://model-test.invalid/v1", http_client=transport, max_retries=0)
            context = suggestions.SuggestionContext(1, 1, None, None, "元投稿", suggestions.SuggestionDraft(shop_name="対象店舗"))
            request = suggestions.ReviewSuggestionRequest(expected_version=1)
            with patch.object(extractor, "_get_client", return_value=client):
                with pytest.raises(suggestions.SuggestionFailure) as failure:
                    await suggestions.suggest_shops(context, request)
                assert failure.value.code == "suggestion_timeout"

    monkeypatch.setattr(suggestions, "SUGGESTION_TIMEOUT_SECONDS", 0.2)
    asyncio.run(exercise())
    assert stopped == [True]

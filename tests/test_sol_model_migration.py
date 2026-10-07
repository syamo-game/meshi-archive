from __future__ import annotations

import asyncio
from collections.abc import Generator

import httpx
import pytest
from openai import AsyncOpenAI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot import restaurant_extractor as extractor
from db.models import Base
from services import identification_pipeline as pipeline


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("setting", ["EXTRACTION_REASONING_EFFORT", "RESOLUTION_REASONING_EFFORT"])
@pytest.mark.parametrize("effort", ["none", "minimal", "invalid", ""])
def test_unsupported_sol_efforts_fail_with_configuration_context(
    monkeypatch: pytest.MonkeyPatch, setting: str, effort: str,
) -> None:
    monkeypatch.setenv(setting, effort)
    with pytest.raises(ValueError, match=f"setting={setting}"):
        extractor._configured_reasoning_effort(setting)


def test_sol_usage_has_a_nonzero_token_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    for setting in ("GPT_6_1_SOL_INPUT_USD_PER_M", "GPT_6_1_SOL_OUTPUT_USD_PER_M"):
        monkeypatch.delenv(setting, raising=False)
    assert extractor._estimate_cost_microusd("gpt-6.1-sol", 1_000, 2_000) == 22_000


@pytest.mark.parametrize("kind", ["web_search", "source_discovery", pipeline.SOURCE_MENTION_CACHE_KIND])
@pytest.mark.parametrize("component", ["MODEL", "REASONING_EFFORT"])
def test_ai_cache_does_not_reuse_a_different_model_or_effort(
    db: Session, monkeypatch: pytest.MonkeyPatch, kind: str, component: str,
) -> None:
    pipeline._put_cache(db, kind, "same evidence", "previous result")
    pipeline._put_cache(db, "url_metadata", "same evidence", "page metadata")
    db.commit()
    assert pipeline._get_cache(db, kind, "same evidence") is not None
    role: str = "RESOLUTION" if kind == "web_search" else "EXTRACTION"
    changed: str = "gpt-5.6-terra" if component == "MODEL" else "medium"
    monkeypatch.setattr(pipeline, f"{role}_{component}", changed)
    assert pipeline._get_cache(db, kind, "same evidence") is None
    page_cache = pipeline._get_cache(db, "url_metadata", "same evidence")
    assert page_cache is not None
    assert page_cache.payload == "page metadata"


def test_identical_sol_preflight_runs_once_with_the_actual_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "resp_preflight", "object": "response", "created_at": 0,
            "model": "gpt-6.1-sol", "status": "completed",
            "output": [{
                "id": "msg_preflight", "type": "message", "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"ok":true}', "annotations": []}],
            }],
        })

    for setting in ("EXTRACTION_MODEL", "RESOLUTION_MODEL"):
        monkeypatch.setattr(extractor, setting, "gpt-6.1-sol")
    for setting in ("EXTRACTION_REASONING_EFFORT", "RESOLUTION_REASONING_EFFORT"):
        monkeypatch.setattr(extractor, setting, "high")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            async with AsyncOpenAI(
                api_key="synthetic-test-key", base_url="https://model-test.invalid/v1",
                http_client=transport, max_retries=0,
            ) as client:
                monkeypatch.setattr(extractor, "_client", client)
                monkeypatch.setattr(extractor, "_OPENAI_API_KEY", "synthetic-test-key")
                await extractor.preflight_models()

    asyncio.run(exercise())
    assert len(requests) == 1
    assert requests[0].url.host == "model-test.invalid"

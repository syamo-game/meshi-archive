from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from bot.restaurant_extractor import (
    CandidateSearchContext, CandidateSearchResult, ExtractedMention, ExtractedMessage,
    ExtractionCallResult, ExtractionError, ModelCallMetrics, SearchCandidate, SearchCandidateSet,
)
from db.models import Base, Message, Shop, ShopMention
from services import review_suggestions as service
from web.routers import review_suggestions as router


@dataclass(frozen=True)
class SuggestionCase:
    client: TestClient
    engine: Engine
    sessions: sessionmaker[Session]
    mention_id: int
    shop_id: int


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[SuggestionCase, None, None]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(f"sqlite:///{(tmp_path / 'suggestions.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions: sessionmaker[Session] = sessionmaker(bind=engine)
    with sessions() as db:
        shop = Shop(shop_name="保存済みの店", branch_name="本店", area="銀座", category="寿司・回転寿司", memo="残すメモ", rating=4)
        message = Message(message_id="123456789012345678", content="変更してはいけない元投稿", processing_status="succeeded")
        mention = ShopMention(message=message, shop=shop, occurrence_index=0, extracted_name="抽出当時の店名", review_status="pending", metadata_review_status="pending", extraction_source="test")
        db.add(mention)
        db.commit()
        mention_id, shop_id = mention.id, shop.id
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-suggestion-session")
    app.include_router(router.router)

    def get_db() -> Generator[Session, None, None]:
        with sessions() as db:
            yield db

    app.dependency_overrides[router.get_db] = get_db

    @app.get("/_test/login")
    def login(request: Request) -> dict[str, str]:
        request.session["admin_authenticated"] = True
        request.session["csrf_token"] = "synthetic-csrf"
        return {"status": "ok"}

    with TestClient(app) as client:
        client.get("/_test/login")
        client.headers["X-CSRF-Token"] = "synthetic-csrf"
        yield SuggestionCase(client, engine, sessions, mention_id, shop_id)
    engine.dispose()


def metrics(*, search: bool = True) -> ModelCallMetrics:
    return ModelCallMetrics(model="test", input_tokens=1, output_tokens=1, web_search_calls=int(search), image_count=0, latency_ms=1, estimated_cost_microusd=1)


def result(*, with_uncited: bool = False) -> CandidateSearchResult:
    candidates = [SearchCandidate(name="候補の店", area="銀座", category="寿司・回転寿司", address="東京都中央区銀座1-2-3", canonical_url="https://example.com/shops/one", evidence_url="https://example.com/shops/one", confidence_reason="公式店舗ページで所在地を確認")]
    if with_uncited:
        candidates.append(SearchCandidate(name="出典のない店", canonical_url="https://invented.example/shop", confidence_reason="根拠なし"))
    return CandidateSearchResult(SearchCandidateSet(candidates=candidates), metrics(), ("https://example.com/shops/one",))


def snapshot(case: SuggestionCase) -> dict[str, tuple[str, ...]]:
    with case.engine.connect() as connection:
        return {table.name: tuple(repr(tuple(row)) for row in connection.execute(select(table))) for table in Base.metadata.sorted_tables}


def endpoint(case: SuggestionCase) -> str:
    return f"/api/admin/reviews/{case.mention_id}/suggestions"


def test_suggestions_preserve_every_table_and_use_current_draft(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[ExtractedMention, CandidateSearchContext]] = []

    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        assert context is not None
        seen.append((mention, context))
        return result(with_uncited=True)

    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    before = snapshot(case)
    response = case.client.post(endpoint(case), json={
        "expected_version": 1, "shop_version": 1,
        "draft": {"shop_name": "手入力した店", "branch_name": None, "address": "追加した住所", "phone": "0312345678"},
        "supplement": "ビルの2階", "reference_url": "https://example.com/reference",
    })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert len(response.json()["candidates"]) == 1
    assert "出典" in response.json()["unresolved_reason"]
    mention, context = seen[0]
    assert mention.shop_name == "手入力した店"
    assert mention.branch_name is None
    assert mention.area == "銀座"
    assert context.address == "追加した住所"
    assert context.phone == "0312345678"
    assert context.supplement == "ビルの2階"
    assert context.reference_urls == ("https://example.com/reference",)
    assert snapshot(case) == before


def test_unresolved_name_extracts_separate_original_and_supplement(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs: list[str] = []

    async def extract(text: str) -> ExtractionCallResult:
        inputs.append(text)
        return ExtractionCallResult(ExtractedMessage(is_restaurant_message=True, mentions=[ExtractedMention(shop_name="発見した店", area="銀座", needs_review=True, confidence_reason="追加情報に店名あり", subject_kind="restaurant")]), metrics(search=False))

    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        assert mention.shop_name == "発見した店"
        assert context is not None and context.supplement == "看板には発見した店と書いてある"
        return result()

    monkeypatch.setattr(service, "extract_restaurant_message", extract)
    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    before = snapshot(case)
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1, "draft": {"shop_name": ""}, "supplement": "看板には発見した店と書いてある"})
    assert response.status_code == 200
    assert "[元の投稿]\n変更してはいけない元投稿" in inputs[0]
    assert "原文とは別" in inputs[0]
    assert snapshot(case) == before


def test_multiple_extracted_shops_require_user_selection(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    async def extract(text: str) -> ExtractionCallResult:
        return ExtractionCallResult(ExtractedMessage(is_restaurant_message=True, mentions=[ExtractedMention(shop_name=name, needs_review=True, confidence_reason="店名あり") for name in ("一軒目", "二軒目")]), metrics(search=False))

    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        pytest.fail("Ambiguous extraction must not select a shop for searching")

    monkeypatch.setattr(service, "extract_restaurant_message", extract)
    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1, "draft": {"shop_name": ""}})
    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert "複数" in response.json()["unresolved_reason"]


@pytest.mark.parametrize("version,shop_version,code", [(2, 1, "stale_mention"), (1, 2, "stale_shop"), (1, None, "stale_shop")])
def test_stale_requests_stop_before_ai(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch, version: int, shop_version: int | None, code: str) -> None:
    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        pytest.fail("Stale requests must not call AI")

    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    response = case.client.post(endpoint(case), json={"expected_version": version, "shop_version": shop_version})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code


def test_external_wait_has_no_database_transaction_and_rechecks_versions(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        with case.sessions() as other:
            shop = other.get(Shop, case.shop_id)
            assert shop is not None
            shop.shop_name = "別操作で保存"
            shop.version += 1
            other.commit()
        return result()

    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_shop"
    with case.sessions() as db:
        assert db.get(Shop, case.shop_id).shop_name == "別操作で保存"


def test_timeout_cancels_ai_and_releases_inflight_slot(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[bool] = []

    async def blocked(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        try:
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")
        finally:
            cancelled.append(True)

    monkeypatch.setattr(service, "SUGGESTION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(service, "search_restaurant_candidates", blocked)
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1})
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "suggestion_timeout"
    assert cancelled == [True]
    with service.reserve_suggestion(case.mention_id):
        pass


def test_failure_preserves_input_and_does_not_leak_upstream_details(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def failing(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        raise ExtractionError("sensitive-upstream-body")

    monkeypatch.setattr(service, "search_restaurant_candidates", failing)
    before = snapshot(case)
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1})
    assert response.status_code == 502
    assert "sensitive-upstream-body" not in response.text + caplog.text
    assert "ExtractionError" in caplog.text
    assert snapshot(case) == before


def test_duplicate_and_capacity_limits_release_after_failure() -> None:
    with pytest.raises(RuntimeError, match="synthetic"):
        with service.reserve_suggestion(10001):
            with pytest.raises(service.SuggestionFailure) as duplicate:
                with service.reserve_suggestion(10001):
                    pass
            assert duplicate.value.code == "suggestion_in_progress"
            with service.reserve_suggestion(10002):
                with pytest.raises(service.SuggestionFailure) as busy:
                    with service.reserve_suggestion(10003):
                        pass
                assert busy.value.status == 429
            raise RuntimeError("synthetic")
    with service.reserve_suggestion(10001):
        pass


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/shop", "http://127.0.0.1/shop", "http://192.168.1.2/shop", "https://user:secret@example.com/shop", "https://example.com:8443/shop"])
def test_invalid_reference_urls_are_rejected(case: SuggestionCase, url: str) -> None:
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1, "reference_url": url})
    assert response.status_code == 422


def test_admin_csrf_read_only_and_strict_input(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"expected_version": 1, "shop_version": 1}
    assert case.client.post(endpoint(case), json={**body, "unexpected": "value"}).status_code == 422
    assert case.client.post(endpoint(case), json={**body, "draft": {"shop_name": 123}}).status_code == 422
    assert case.client.post(endpoint(case), json={**body, "supplement": "x" * 4001}).status_code == 422
    assert case.client.post(endpoint(case), json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    monkeypatch.setenv("APP_READ_ONLY", "true")
    assert case.client.post(endpoint(case), json=body).status_code == 503
    monkeypatch.setenv("APP_READ_ONLY", "false")
    case.client.cookies.clear()
    assert case.client.post(endpoint(case), json=body).status_code == 403


def test_excluded_item_cannot_start_suggestions(case: SuggestionCase) -> None:
    with case.sessions() as db:
        mention = db.get(ShopMention, case.mention_id)
        assert mention is not None
        mention.review_status = "rejected"
        db.commit()
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "suggestion_excluded"


def test_invalid_stored_url_fails_clearly_and_can_be_cleared(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    with case.sessions() as db:
        shop = db.get(Shop, case.shop_id)
        assert shop is not None
        shop.canonical_url = "http://127.0.0.1/internal"
        db.commit()

    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        assert context is not None and context.reference_urls == ()
        return result()

    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "suggestion_invalid_input"
    fixed = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1, "draft": {"canonical_url": None}})
    assert fixed.status_code == 200
    with case.sessions() as db:
        assert db.get(Shop, case.shop_id).canonical_url == "http://127.0.0.1/internal"


def test_changed_supplements_always_reach_search(case: SuggestionCase, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def search(mention: ExtractedMention, *, context: CandidateSearchContext | None = None) -> CandidateSearchResult:
        assert context is not None
        seen.append(context.supplement)
        return result()

    monkeypatch.setattr(service, "search_restaurant_candidates", search)
    for value in ("1階", "2階"):
        response = case.client.post(endpoint(case), json={"expected_version": 1, "shop_version": 1, "supplement": value})
        assert response.status_code == 200
    assert seen == ["1階", "2階"]

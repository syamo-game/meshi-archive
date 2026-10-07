from __future__ import annotations

import json
import re
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import AdminSession, Base, DiscordViewer, DiscordViewerGrantEvent, Message, ReviewEvent, Shop, ShopMention, SourceAsset
from services.admin_sessions import is_discord_admin, issue_admin_session
from services.discord_access import discord_grant_generation
from web import discord_session
from web.routers import admin, auth_discord, home, review

ADMIN_ID = "11111111111111111"
VIEWER_ID = "22222222222222222"
NEW_ID = "33333333333333333"


@dataclass(frozen=True)
class AppState:
    client: TestClient
    engine: Engine


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> Generator[AppState, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(discord_session, "SessionLocal", factory)
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", "")
    monkeypatch.setenv("APP_READ_ONLY", "false")
    with factory() as db:
        db.add(DiscordViewer(discord_user_id=VIEWER_ID))
        db.commit()
    app = FastAPI()
    app.add_middleware(discord_session.DiscordGrantMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="synthetic-admin-improvement-secret", session_cookie="meshi_session")
    app.include_router(admin.router, prefix="/admin")
    app.include_router(review.router)
    app.include_router(home.router)

    def database() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    @app.get("/_test/login/{user_id}")
    def login(user_id: str, request: Request) -> dict[str, str]:
        with factory() as db:
            if is_discord_admin(db, user_id, {ADMIN_ID}):
                issue_admin_session(db, request.session, method="discord", credential=user_id, discord_user_id=user_id)
            else:
                request.session.clear()
                request.session.update(
                    authenticated=True, discord_user_id=user_id,
                    discord_grant_generation=discord_grant_generation(db, user_id, {ADMIN_ID}),
                )
        request.session["csrf_token"] = "improvement-token"
        return {"csrf_token": "improvement-token"}

    @app.get("/_test/private")
    def private(request: Request) -> dict[str, bool]:
        return {"allowed": bool(request.session.get("authenticated"))}

    app.dependency_overrides[admin.get_db] = database
    app.dependency_overrides[review.get_db] = database
    app.dependency_overrides[home.get_db] = database
    with TestClient(app) as client:
        yield AppState(client, engine)
    engine.dispose()


def test_revoke_replace_and_regrant_preserve_audit_and_never_revive_old_cookie(state: AppState) -> None:
    client = state.client
    client.get(f"/_test/login/{VIEWER_ID}")
    old_cookie = client.cookies.get("meshi_session")
    assert client.get("/_test/private").json()["allowed"] is True
    client.get(f"/_test/login/{ADMIN_ID}")
    with Session(state.engine) as db:
        generation = db.get(DiscordViewer, VIEWER_ID).generation
    endpoint = f"/admin/users/{VIEWER_ID}/change"
    payload = {"csrf_token": "improvement-token", "expected_generation": generation, "action": "replace", "replacement_user_id": NEW_ID}
    assert client.post(endpoint, data={**payload, "csrf_token": "wrong"}).status_code == 403
    assert client.post(endpoint, data={**payload, "expected_generation": "stale"}).status_code == 409
    assert client.post(endpoint, data=payload).status_code == 200
    with Session(state.engine) as db:
        assert db.get(DiscordViewer, VIEWER_ID) is None
        assert db.get(DiscordViewer, NEW_ID) is not None
        event = db.scalar(select(DiscordViewerGrantEvent).where(DiscordViewerGrantEvent.action == "replace"))
        assert (event.discord_user_id, event.replacement_user_id, event.actor_discord_user_id) == (VIEWER_ID, NEW_ID, ADMIN_ID)
    client.post("/admin/users", data={"discord_user_id": VIEWER_ID, "csrf_token": "improvement-token"})
    client.cookies.clear()
    client.cookies.set("meshi_session", old_cookie)
    assert client.get("/_test/private").json()["allowed"] is False
    client.cookies.clear()
    client.get(f"/_test/login/{VIEWER_ID}")
    assert client.get("/_test/private").json()["allowed"] is True
    client.get(f"/_test/login/{ADMIN_ID}")
    with Session(state.engine) as db:
        generation = db.get(DiscordViewer, VIEWER_ID).generation
    assert client.post(endpoint, data={"csrf_token": "improvement-token", "expected_generation": generation, "action": "revoke"}).status_code == 200
    with Session(state.engine) as db:
        assert db.get(DiscordViewer, VIEWER_ID) is None
        assert db.scalar(select(DiscordViewerGrantEvent).where(DiscordViewerGrantEvent.action == "revoke")) is not None


def test_configured_admin_cannot_be_revoked_from_users_screen(state: AppState) -> None:
    state.client.get(f"/_test/login/{ADMIN_ID}")
    result = state.client.post(f"/admin/users/{ADMIN_ID}/change", data={
        "csrf_token": "improvement-token", "expected_generation": "configuration", "action": "revoke",
    })
    assert result.status_code == 409
    assert state.client.get("/admin/users").status_code == 200


def test_combined_queue_deduplicates_and_exclusion_keeps_shop_post_assets_and_history(state: AppState) -> None:
    with Session(state.engine) as db:
        shop = Shop(shop_name="Shared shop", area=None, category=None, memo="Keep memo", rating=4)
        post = Message(message_id="44444444444444444", content="Original source")
        mentions = [
            ShopMention(message=post, shop=shop, occurrence_index=i, extracted_name=shop.shop_name,
                        review_status="pending", metadata_review_status="pending", extraction_source="test")
            for i in range(2)
        ]
        db.add_all(mentions)
        db.add(SourceAsset(message=post, kind="image", url="https://example.test/evidence.png"))
        db.commit()
        mention_id, shop_id = mentions[0].id, shop.id
    client = state.client
    client.get(f"/_test/login/{ADMIN_ID}")
    first = client.get("/api/admin/reviews").json()
    assert first["scope"] == "all"
    assert first["total_count"] == first["counters"]["unresolved"] == 2
    assert {"missing_area", "missing_category"} <= {issue["code"] for issue in first["items"][0]["issues"]}
    response = client.post(f"/api/admin/reviews/{mention_id}/decision", headers={"X-CSRF-Token": "improvement-token"}, json={
        "action": "exclude", "expected_version": 1, "shop_version": 1, "reason": "Out of scope",
    })
    assert response.status_code == 200
    assert response.json()["shop_id"] == shop_id
    assert client.get("/api/admin/reviews").json()["counters"]["unresolved"] == 1
    assert client.get("/api/admin/reviews?scope=metadata").json()["total_count"] == 1
    with Session(state.engine) as db:
        assert (db.query(Message).count(), db.query(SourceAsset).count(), db.query(ShopMention).count()) == (1, 1, 2)
        assert db.get(Shop, shop_id).memo == "Keep memo"
        assert db.get(Shop, shop_id).rating == 4
        event = db.scalar(select(ReviewEvent).where(ReviewEvent.mention_id == mention_id))
        assert event.action == "exclude"
        assert json.loads(event.note)["preserved_shop"]["id"] == shop_id
    assert client.post(f"/api/admin/reviews/{mention_id}/decision", headers={"X-CSRF-Token": "improvement-token"}, json={
        "action": "exclude", "expected_version": 1, "shop_version": 1,
    }).status_code == 409


def test_failed_post_can_be_prepared_once_then_registered_with_reference_history(state: AppState) -> None:
    message_id = "55555555555555555"
    with Session(state.engine) as db:
        db.add(Message(message_id=message_id, content="Untouched original post", processing_status="failed"))
        db.commit()
    client = state.client
    client.get(f"/_test/login/{ADMIN_ID}")
    failures = client.get("/api/admin/review-failures").json()
    assert failures["items"][0]["mention_ids"] == []
    assert failures["items"][0]["recovery_status"] == "not_prepared"
    endpoint = f"/api/admin/review-failures/{message_id}/prepare"
    assert client.post(endpoint).status_code == 403
    headers = {"X-CSRF-Token": "improvement-token"}
    prepared = client.post(endpoint, headers=headers).json()
    assert client.get("/api/admin/review-failures").json()["items"][0]["recovery_status"] == "in_review"
    assert client.post(endpoint, headers=headers).json()["id"] == prepared["id"]
    saved = client.post(f"/api/admin/reviews/{prepared['id']}/decision", headers=headers, json={
        "action": "edit_and_approve", "expected_version": prepared["version"],
        "shop": {"shop_name": "確認済み店", "area": "銀座", "category": "寿司・回転寿司"},
        "supplement": "Manually supplied details",
        "suggestion_evidence_urls": ["https://example.test/shop"],
        "suggestion_reason": "User selected a proposal",
    })
    assert saved.status_code == 200
    assert client.get("/api/admin/review-failures").json()["items"][0]["recovery_status"] == "handled"
    assert client.get("/api/admin/review-failures?active_only=true").json()["items"] == []
    assert client.get("/api/admin/reviews").json()["counters"]["unresolved"] == 0
    with Session(state.engine) as db:
        assert db.query(ShopMention).count() == 1
        assert db.get(Message, message_id).content == "Untouched original post"
        event = db.scalar(select(ReviewEvent).where(ReviewEvent.action == "edit_and_approve"))
        note = json.loads(event.note)
        assert note["supplement"] == "Manually supplied details"
        assert note["selected_reference_urls"] == ["https://example.test/shop"]
        assert note["after"]["shop_name"] == "確認済み店"


def test_combined_queue_places_deferred_identity_only_in_deferred_filter(state: AppState) -> None:
    with Session(state.engine) as db:
        mention = ShopMention(message=Message(message_id="77777777777777777", content="Original post"),
                              shop=Shop(shop_name="Deferred shop"), occurrence_index=0, extracted_name="Deferred shop",
                              review_status="deferred", metadata_review_status="pending", extraction_source="test")
        db.add(mention)
        db.commit()
        mention_id = mention.id
    state.client.get(f"/_test/login/{ADMIN_ID}")
    pending = state.client.get("/api/admin/reviews?scope=all&status=pending").json()
    deferred = state.client.get("/api/admin/reviews?scope=all&status=deferred").json()
    assert pending["total_count"] == 0
    assert deferred["total_count"] == deferred["counters"]["unresolved"] == 1
    assert deferred["items"][0]["id"] == mention_id
    assert state.client.get("/api/admin/reviews?scope=metadata&status=pending").json()["total_count"] == 1


@pytest.mark.parametrize("path,title", [
    ("/admin", "概要"), ("/admin/users", "利用ユーザー"),
    ("/admin/shops/bulk", "店舗情報の一括編集"), ("/admin/exports", "データ出力"),
    ("/admin/review", "登録内容の確認"),
])
def test_admin_pages_have_plain_titles_unique_controls_and_correct_destination(state: AppState, path: str, title: str) -> None:
    state.client.get(f"/_test/login/{ADMIN_ID}")
    response = state.client.get(path)
    assert response.status_code == 200
    assert f"<title>{title} — Meshi Archive</title>" in response.text
    ids = re.findall(r'\bid="([^"]+)"', response.text)
    assert len(ids) == len(set(ids)), "Duplicate IDs break labels, form controls, and JS selection"
    assert 'aria-current="page"' in response.text and "サイトを見る" not in response.text
    if path == "/admin/users":
        assert response.text.index('id="allowed-users-heading"') < response.text.index('id="add-user-heading"')
        assert response.text.count('action="/admin/users"') == 1
        assert "formnovalidate" in response.text
    elif path == "/admin/exports":
        for endpoint in ("/export.csv", "/admin/review/audit.csv"):
            assert f'href="{endpoint}"' in response.text
            download = state.client.get(endpoint)
            assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    elif path == "/admin/shops/bulk":
        assert 'action="/admin/import/validate"' in response.text
        assert 'href="/admin/import/update/template.csv"' in response.text
        assert state.client.get("/admin/import/update/template.csv").status_code == 200
    elif path == "/admin":
        assert "登録できなかった投稿" in response.text
        assert "対応履歴" not in response.text
        assert "確認項目を作成" not in response.text
        assert 'href="/admin/review"' in response.text
        assert 'id="import-form"' not in response.text


def _user_generation(state: AppState, user_id: str = VIEWER_ID) -> str:
    with Session(state.engine) as db:
        viewer: DiscordViewer | None = db.get(DiscordViewer, user_id)
        assert viewer is not None
        return viewer.generation


def _save_user(state: AppState, *, user_id: str = VIEWER_ID, role: str = "admin",
               replacement: str = VIEWER_ID, generation: str | None = None) -> Response:
    return state.client.post(f"/admin/users/{user_id}/change", data={
        "csrf_token": "improvement-token", "expected_generation": generation or _user_generation(state, user_id),
        "action": "save", "replacement_user_id": replacement, "role": role,
    })


def test_role_save_promotes_only_after_login_and_repromotion_never_revives_old_admin_cookie(state: AppState) -> None:
    client = state.client
    client.get(f"/_test/login/{VIEWER_ID}")
    old_viewer_cookie = client.cookies.get("meshi_session")
    client.cookies.clear()
    client.get(f"/_test/login/{ADMIN_ID}")
    assert _save_user(state).status_code == 200
    with Session(state.engine) as db:
        assert db.get(DiscordViewer, VIEWER_ID).is_admin is True
        event = db.scalar(select(DiscordViewerGrantEvent))
        assert (event.action, event.previous_role, event.new_role, event.actor_discord_user_id) == ("update", "viewer", "admin", ADMIN_ID)
    client.cookies.clear()
    client.cookies.set("meshi_session", old_viewer_cookie, domain="testserver.local", path="/")
    assert client.get("/_test/private").json()["allowed"] is False
    client.get(f"/_test/login/{VIEWER_ID}")
    assert client.get("/admin/users").status_code == 200
    old_admin_cookie = client.cookies.get("meshi_session")
    client.cookies.clear()
    client.get(f"/_test/login/{ADMIN_ID}")
    assert _save_user(state, role="viewer").status_code == 200
    with Session(state.engine) as db:
        assert db.scalar(select(AdminSession).where(AdminSession.actor_discord_user_id == VIEWER_ID)) is None
    client.cookies.clear()
    client.cookies.set("meshi_session", old_admin_cookie, domain="testserver.local", path="/")
    assert client.get("/admin/users", follow_redirects=False).status_code == 302
    client.cookies.clear()
    client.get(f"/_test/login/{ADMIN_ID}")
    assert _save_user(state).status_code == 200
    client.cookies.clear()
    client.cookies.set("meshi_session", old_admin_cookie, domain="testserver.local", path="/")
    assert client.get("/admin/users", follow_redirects=False).status_code == 302
    client.get(f"/_test/login/{VIEWER_ID}")
    assert client.get("/admin/users").status_code == 200


def test_id_and_role_save_is_atomic_and_retains_original_registration_time(state: AppState) -> None:
    with Session(state.engine) as db:
        created_at = db.get(DiscordViewer, VIEWER_ID).created_at
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    assert _save_user(state, replacement=NEW_ID).status_code == 200
    with Session(state.engine) as db:
        assert db.get(DiscordViewer, VIEWER_ID) is None
        viewer = db.get(DiscordViewer, NEW_ID)
        assert viewer.is_admin is True and viewer.created_at == created_at
        event = db.scalar(select(DiscordViewerGrantEvent))
        assert (event.discord_user_id, event.replacement_user_id, event.previous_role, event.new_role) == (VIEWER_ID, NEW_ID, "viewer", "admin")
    state.client.get(f"/_test/login/{NEW_ID}")
    assert state.client.get("/admin/users").status_code == 200


@pytest.mark.parametrize("role,replacement,generation", [
    ("owner", VIEWER_ID, None), ("admin", "invalid", None),
    ("admin", ADMIN_ID, None), ("admin", VIEWER_ID, "outdated"),
])
def test_rejected_save_preserves_table_inputs_grant_and_audit(
    state: AppState, role: str, replacement: str, generation: str | None,
) -> None:
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    before = _user_generation(state)
    response = _save_user(state, role=role, replacement=replacement, generation=generation)
    assert response.status_code == 409
    assert 'id="discord-users"' in response.text and f'value="{replacement}"' in response.text
    with Session(state.engine) as db:
        assert db.get(DiscordViewer, VIEWER_ID).is_admin is False
        assert db.get(DiscordViewer, VIEWER_ID).generation == before
        assert db.query(DiscordViewerGrantEvent).count() == 0


def test_unchanged_save_retains_generation_and_existing_session(state: AppState) -> None:
    state.client.get(f"/_test/login/{VIEWER_ID}")
    cookie = state.client.cookies.get("meshi_session")
    before = _user_generation(state)
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    assert _save_user(state, role="viewer").status_code == 200
    assert _user_generation(state) == before
    with Session(state.engine) as db:
        assert db.query(DiscordViewerGrantEvent).count() == 0
    state.client.cookies.clear()
    state.client.cookies.set("meshi_session", cookie, domain="testserver.local", path="/")
    assert state.client.get("/_test/private").json()["allowed"] is True


def test_role_audit_failure_rolls_back_access_generation_and_session_revocation(state: AppState) -> None:
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    assert _save_user(state).status_code == 200
    state.client.get(f"/_test/login/{VIEWER_ID}")
    cookie = state.client.cookies.get("meshi_session")
    generation = _user_generation(state)
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    with state.engine.begin() as connection:
        connection.execute(text("CREATE TRIGGER reject_role_audit BEFORE INSERT ON discord_viewer_grant_events BEGIN SELECT RAISE(ABORT, 'synthetic audit rejection'); END"))
    assert _save_user(state, role="viewer").status_code == 503
    with Session(state.engine) as db:
        viewer = db.get(DiscordViewer, VIEWER_ID)
        assert viewer.is_admin is True and viewer.generation == generation
        assert db.scalar(select(AdminSession).where(AdminSession.actor_discord_user_id == VIEWER_ID)) is not None
        assert db.query(DiscordViewerGrantEvent).count() == 1
    state.client.cookies.clear()
    state.client.cookies.set("meshi_session", cookie, domain="testserver.local", path="/")
    assert state.client.get("/admin/users").status_code == 200


@pytest.mark.parametrize("action", ["save", "revoke"])
def test_user_changes_require_real_admin_csrf_and_writable_mode(state: AppState, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    payload = {"csrf_token": "improvement-token", "expected_generation": _user_generation(state),
               "action": action, "replacement_user_id": VIEWER_ID, "role": "admin"}
    endpoint = f"/admin/users/{VIEWER_ID}/change"
    state.client.get(f"/_test/login/{VIEWER_ID}")
    assert state.client.post(endpoint, data=payload, follow_redirects=False).status_code == 302
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    assert state.client.post(endpoint, data={**payload, "csrf_token": "bad"}).status_code == 403
    monkeypatch.setenv("APP_READ_ONLY", "true")
    assert state.client.post(endpoint, data=payload).status_code == 503
    with Session(state.engine) as db:
        assert db.get(DiscordViewer, VIEWER_ID).is_admin is False
        assert db.query(DiscordViewerGrantEvent).count() == 0


def test_fixed_initial_admin_cannot_be_changed_by_role_save(state: AppState) -> None:
    state.client.cookies.clear()
    state.client.get(f"/_test/login/{ADMIN_ID}")
    response = state.client.post(f"/admin/users/{ADMIN_ID}/change", data={
        "csrf_token": "improvement-token", "expected_generation": "configuration", "action": "save",
        "replacement_user_id": ADMIN_ID, "role": "viewer",
    })
    assert response.status_code == 409
    assert state.client.get("/admin/users").status_code == 200


def test_active_failed_posts_paginate_before_limit_and_link_to_open_mention(state: AppState) -> None:
    message_ids = [str(88000000000000000 + index) for index in range(5)]
    with Session(state.engine) as db:
        posts = [Message(message_id=message_id, content=f"Original {index}", processing_status="failed")
                 for index, message_id in enumerate(message_ids)]
        db.add_all(posts)
        closed = ShopMention(message=posts[0], occurrence_index=0, extracted_name="Registered",
                             review_status="approved", metadata_review_status="approved")
        excluded = ShopMention(message=posts[1], occurrence_index=0, extracted_name="Excluded",
                               review_status="rejected", metadata_review_status="pending", difference_type="manual_excluded")
        mixed_closed = ShopMention(message=posts[3], occurrence_index=0, extracted_name="Already handled",
                                   review_status="approved", metadata_review_status="approved")
        mixed_open = ShopMention(message=posts[3], occurrence_index=1, extracted_name="Still pending",
                                 review_status="pending", metadata_review_status="pending")
        metadata_open = ShopMention(message=posts[4], occurrence_index=0, extracted_name="Metadata pending",
                                    review_status="approved", metadata_review_status="deferred")
        db.add_all([closed, excluded, mixed_closed, mixed_open, metadata_open])
        db.commit()
        open_id, metadata_id = mixed_open.id, metadata_open.id
    client = state.client
    client.get(f"/_test/login/{ADMIN_ID}")
    page = client.get("/api/admin/review-failures?active_only=true&limit=1").json()
    assert [item["message_id"] for item in page["items"]] == [message_ids[2]]
    assert page["items"][0]["review_mention_id"] is None
    assert page["next_cursor"] == message_ids[2]
    page = client.get(f"/api/admin/review-failures?active_only=true&limit=1&cursor={page['next_cursor']}").json()
    assert page["items"][0]["message_id"] == message_ids[3]
    assert page["items"][0]["review_mention_id"] == open_id
    assert page["next_cursor"] == message_ids[3]
    page = client.get(f"/api/admin/review-failures?active_only=true&limit=1&cursor={page['next_cursor']}").json()
    assert page["items"][0]["review_mention_id"] == metadata_id
    assert page["next_cursor"] is None
    assert len(client.get("/api/admin/review-failures").json()["items"]) == 5
    with Session(state.engine) as db:
        assert db.query(Message).count() == db.query(ShopMention).count() == 5
        assert [db.get(Message, message_id).content for message_id in message_ids] == [f"Original {index}" for index in range(5)]


def test_excluded_failed_post_leaves_overview_but_keeps_source_and_audit(state: AppState) -> None:
    message_id = "89999999999999999"
    with Session(state.engine) as db:
        db.add(Message(message_id=message_id, content="Preserved failed post", processing_status="failed"))
        db.commit()
    client = state.client
    client.get(f"/_test/login/{ADMIN_ID}")
    headers = {"X-CSRF-Token": "improvement-token"}
    prepared = client.post(f"/api/admin/review-failures/{message_id}/prepare", headers=headers).json()
    excluded = client.post(f"/api/admin/reviews/{prepared['id']}/decision", headers=headers, json={
        "action": "exclude", "expected_version": prepared["version"],
    })
    assert excluded.status_code == 200
    assert client.get("/api/admin/review-failures?active_only=true").json()["items"] == []
    with Session(state.engine) as db:
        assert db.get(Message, message_id).content == "Preserved failed post"
        assert db.get(ShopMention, prepared["id"]) is not None
        assert db.scalar(select(ReviewEvent).where(ReviewEvent.mention_id == prepared["id"], ReviewEvent.action == "exclude")) is not None


def test_saved_user_redirects_to_get_and_refresh_does_not_repeat_the_write(state: AppState) -> None:
    state.client.get(f"/_test/login/{ADMIN_ID}")
    response = state.client.post(f"/admin/users/{VIEWER_ID}/change", follow_redirects=False, data={
        "csrf_token": "improvement-token", "expected_generation": _user_generation(state),
        "action": "save", "replacement_user_id": VIEWER_ID, "role": "admin",
    })
    assert response.status_code == 303 and response.headers["location"] == "/admin/users"
    assert "保存しました" in state.client.get("/admin/users").text
    assert "保存しました" not in state.client.get("/admin/users").text
    with Session(state.engine) as db:
        assert db.query(DiscordViewerGrantEvent).count() == 1

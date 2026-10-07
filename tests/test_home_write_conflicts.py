from __future__ import annotations

import io
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, SourceAsset
from services.import_service import ImportValidationFailure, apply_csv_update, parse_csv_update
from web.routers import home


MESSAGE_ID = "12345678901234568"


@pytest.fixture
def writer_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path / "photos"))
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'home-writers.sqlite').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ShopMention(
            id=1,
            message=Message(message_id=MESSAGE_ID, content="Saved evidence"),
            shop=Shop(
                id=1, shop_name="Saved shop", area="銀座", category="カフェ",
                memo="Saved memo", rating=3, image_key="a" * 64,
            ),
            occurrence_index=0, extracted_name="Saved shop", review_status="approved",
            metadata_review_status="approved", resolution_status="resolved",
        ))
        db.add(Shop(id=2, shop_name="Untouched shop", memo="Untouched memo"))
        db.add(SourceAsset(message_id=MESSAGE_ID, kind="image", url="https://example.com/saved.png"))
        db.add(ReviewEvent(mention_id=1, action="approve_current", note="Saved history"))
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def writer_app(engine: Engine) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def add_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.scope["session"] = {
            "authenticated": True, "admin_authenticated": True, "csrf_token": "writer-token",
        }
        return await call_next(request)

    def get_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.include_router(home.router)
    app.dependency_overrides[home.get_db] = get_db
    return app


def csv_update(version: int) -> bytes:
    return (
        "_id,shop.version,message_id,memo,rating\n"
        f"1,{version},{MESSAGE_ID},CSV update {version},{version + 3}\n"
    ).encode("utf-8")


def related_snapshot(db: Session) -> dict[str, list[tuple[object, ...]]]:
    return {
        name: [tuple(row) for row in db.execute(table.select()).all()]
        for name, table in Base.metadata.tables.items() if name != "shops"
    }


class FormValues(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("name"):
            self.inputs[values["name"] or ""] = values.get("value") or ""


def test_edit_snapshot_returns_current_csv_values_without_writing_the_database(writer_engine: Engine) -> None:
    with TestClient(writer_app(writer_engine)) as client:
        initial = client.get("/shop/1/edit-snapshot")
        assert initial.status_code == 200
        assert initial.json()["version"] == 1
        with Session(writer_engine) as other:
            apply_csv_update(other, parse_csv_update(csv_update(1), "other-user.csv"))
            saved = [tuple(row) for row in other.execute(Shop.__table__.select()).all()]
            related = related_snapshot(other)
        response = client.get("/shop/1/edit-snapshot")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json() == {
            "version": 2,
            "values": {
                "shop_name": "Saved shop", "area": "銀座", "category": "カフェ",
                "url": "", "address": "", "phone": "", "memo": "CSV update 1", "rating": "4",
                "is_visited": False, "visited_at": "",
            },
            "image_url": f"/media/shop-uploads/{'a' * 64}.webp",
        }
        assert client.get("/shop/999999/edit-snapshot").status_code == 404
    with Session(writer_engine) as verified:
        assert [tuple(row) for row in verified.execute(Shop.__table__.select()).all()] == saved
        assert related_snapshot(verified) == related


@pytest.mark.parametrize("endpoint", ["edit-html", "edit-xhr", "visited", "rating"])
def test_save_from_page_loaded_before_csv_update_preserves_the_newer_data(
    writer_engine: Engine, endpoint: str,
) -> None:
    with TestClient(writer_app(writer_engine)) as client:
        opened = client.get("/shop/1")
        assert opened.status_code == 200
        fields = FormValues()
        fields.feed(opened.text)
        assert fields.inputs["expected_version"] == "1"
        with Session(writer_engine) as other:
            apply_csv_update(other, parse_csv_update(csv_update(1), "other-user.csv"))
            related = related_snapshot(other)

        payload = {
            "shop_name": "My unsaved name", "area": "銀座", "category": "カフェ",
            "address": "My unsaved address", "memo": "My unsaved memo", "rating": "2",
            "csrf_token": "writer-token", "expected_version": fields.inputs["expected_version"],
        }
        if endpoint.startswith("edit-"):
            headers = {} if endpoint == "edit-html" else {"X-Requested-With": "XMLHttpRequest"}
            response = client.post(
                "/shop/1/edit", data=payload, headers=headers, follow_redirects=False,
            )
        else:
            response = client.post(
                f"/shop/1/{endpoint}", json={"rating": 2},
                headers={"X-CSRF-Token": "writer-token", "X-Shop-Version": fields.inputs["expected_version"]},
            )
        assert response.status_code == 409, response.text
        assert response.headers["cache-control"] == "private, no-store"
        if endpoint == "edit-html":
            retained = FormValues()
            retained.feed(response.text)
            assert retained.inputs["shop_name"] == payload["shop_name"]
            assert retained.inputs["address"] == payload["address"]
            assert payload["memo"] in response.text
            assert retained.inputs["expected_version"] == "1"
            repeated = client.post(
                "/shop/1/edit", data={**payload, **retained.inputs}, follow_redirects=False,
            )
            assert repeated.status_code == 409
        else:
            assert "更新されています" in response.json()["detail"]

        reopened = FormValues()
        reopened.feed(client.get("/shop/1").text)
        assert reopened.inputs["expected_version"] == "2"
    with Session(writer_engine) as verified:
        shop = verified.get(Shop, 1)
        assert shop is not None
        assert (shop.version, shop.shop_name, shop.memo, shop.rating, shop.is_visited) == (
            2, "Saved shop", "CSV update 1", 4, False,
        )
        assert shop.image_key == "a" * 64
        assert related_snapshot(verified) == related


@pytest.mark.parametrize("endpoint", ["edit-html", "edit-xhr", "visited", "rating"])
@pytest.mark.parametrize("version", [None, "", "invalid", "0", "-1", "1.5", "2"])
def test_missing_invalid_or_mismatched_page_version_cannot_modify_a_shop(
    writer_engine: Engine, endpoint: str, version: str | None,
) -> None:
    with Session(writer_engine) as before:
        saved = [tuple(row) for row in before.execute(Shop.__table__.select()).all()]
        related = related_snapshot(before)
    with TestClient(writer_app(writer_engine)) as client:
        if endpoint.startswith("edit-"):
            payload = {"shop_name": "Unsaved edit", "memo": "Unsaved memo", "csrf_token": "writer-token"}
            if version is not None:
                payload["expected_version"] = version
            headers = {} if endpoint == "edit-html" else {"X-Requested-With": "XMLHttpRequest"}
            response = client.post("/shop/1/edit", data=payload, headers=headers, follow_redirects=False)
        else:
            headers = {"X-CSRF-Token": "writer-token"}
            if version is not None:
                headers["X-Shop-Version"] = version
            response = client.post(f"/shop/1/{endpoint}", json={"rating": 2}, headers=headers)
    assert response.status_code == 409, response.text
    with Session(writer_engine) as verified:
        assert [tuple(row) for row in verified.execute(Shop.__table__.select()).all()] == saved
        assert related_snapshot(verified) == related


@pytest.mark.parametrize("confirmation", ["complete", "unchecked", "incomplete", "invalid", "stale"])
def test_html_conflict_recovery_requires_explicit_choices_against_the_current_version(
    writer_engine: Engine, confirmation: str,
) -> None:
    with TestClient(writer_app(writer_engine)) as client:
        opened = FormValues()
        opened.feed(client.get("/shop/1").text)
        payload: dict[str, str | list[str]] = {
            "shop_name": "My unsaved name", "area": "銀座", "category": "カフェ",
            "address": "My unsaved address", "memo": "My unsaved memo", "rating": "2",
            "csrf_token": "writer-token", "expected_version": opened.inputs["expected_version"],
        }
        with Session(writer_engine) as other:
            apply_csv_update(other, parse_csv_update(csv_update(1), "other-user.csv"))
        conflicted = client.post("/shop/1/edit", data=payload, follow_redirects=False)
        assert conflicted.status_code == 409
        retained = FormValues()
        retained.feed(conflicted.text)
        assert retained.inputs["confirmed_version"] == "2"
        payload.update({
            "confirmed_version": retained.inputs["confirmed_version"], "confirm_conflict": "on",
            "conflict_choices": ["shop_name:input", "address:input", "memo:latest", "rating:latest"],
        })
        if confirmation == "unchecked":
            payload.pop("confirm_conflict")
        elif confirmation == "incomplete":
            payload["conflict_choices"] = ["shop_name:input", "address:input", "memo:latest"]
        elif confirmation == "invalid":
            payload["conflict_choices"] = ["shop_name:input", "address:input", "memo:latest", "rating:other"]
        elif confirmation == "stale":
            with Session(writer_engine) as other:
                apply_csv_update(other, parse_csv_update(csv_update(2), "newer-other-user.csv"))
        with Session(writer_engine) as before:
            related = related_snapshot(before)
        response = client.post("/shop/1/edit", data=payload, follow_redirects=False)
        assert response.status_code == (302 if confirmation == "complete" else 409), response.text
        if confirmation != "complete":
            unchanged = FormValues()
            unchanged.feed(response.text)
            assert unchanged.inputs["expected_version"] == "1"
            assert unchanged.inputs["confirmed_version"] == ("3" if confirmation == "stale" else "2")
            assert unchanged.inputs["shop_name"] == "My unsaved name"
            assert "My unsaved memo" in response.text
    with Session(writer_engine) as verified:
        shop = verified.get(Shop, 1)
        assert shop is not None
        if confirmation == "complete":
            assert (shop.version, shop.shop_name, shop.address, shop.memo, shop.rating) == (
                3, "My unsaved name", "My unsaved address", "CSV update 1", 4,
            )
        else:
            version = 3 if confirmation == "stale" else 2
            assert (shop.version, shop.shop_name, shop.address, shop.memo, shop.rating) == (
                version, "Saved shop", None, f"CSV update {version - 1}", version + 2,
            )
        assert shop.image_key == "a" * 64
        assert related_snapshot(verified) == related


@pytest.mark.parametrize("endpoint", ["visited", "rating"])
def test_inline_save_returns_a_version_for_next_save_and_rejects_duplicate_request(
    writer_engine: Engine, endpoint: str,
) -> None:
    with TestClient(writer_app(writer_engine)) as client:
        headers = {"X-CSRF-Token": "writer-token", "X-Shop-Version": "1"}
        first = client.post(f"/shop/1/{endpoint}", json={"rating": 4}, headers=headers)
        assert first.status_code == 200
        assert first.json()["version"] == 2
        duplicate = client.post(f"/shop/1/{endpoint}", json={"rating": 4}, headers=headers)
        assert duplicate.status_code == 409
        headers["X-Shop-Version"] = str(first.json()["version"])
        second = client.post(f"/shop/1/{endpoint}", json={"rating": 5}, headers=headers)
        assert second.status_code == 200
        assert second.json()["version"] == 3
    with Session(writer_engine) as verified:
        shop = verified.get(Shop, 1)
        assert shop is not None and shop.version == 3
        if endpoint == "visited":
            assert shop.is_visited is False and shop.visited_at is None
        else:
            assert shop.rating == 5


def test_current_version_input_is_not_replaced_by_leftover_html_conflict_choices(writer_engine: Engine) -> None:
    with TestClient(writer_app(writer_engine)) as client:
        with Session(writer_engine) as other:
            apply_csv_update(other, parse_csv_update(csv_update(1), "other-user.csv"))
            related = related_snapshot(other)
        payload: dict[str, str | list[str]] = {
            "shop_name": "Saved shop", "area": "銀座", "category": "カフェ",
            "memo": "My initial memo", "rating": "2", "csrf_token": "writer-token", "expected_version": "1",
        }
        conflicted = client.post("/shop/1/edit", data=payload, follow_redirects=False)
        assert conflicted.status_code == 409
        retained = FormValues()
        retained.feed(conflicted.text)
        snapshot = client.get("/shop/1/edit-snapshot")
        assert snapshot.status_code == 200
        payload.update({
            "expected_version": str(snapshot.json()["version"]), "memo": "My newly edited memo",
            "confirmed_version": retained.inputs["confirmed_version"], "confirm_conflict": "on",
            "conflict_choices": ["memo:latest", "rating:latest"],
        })
        saved = client.post("/shop/1/edit", data=payload, follow_redirects=False)
        assert saved.status_code == 302
    with Session(writer_engine) as verified:
        shop = verified.get(Shop, 1)
        assert shop is not None
        assert (shop.version, shop.memo, shop.rating) == (3, "My newly edited memo", 2)
        assert related_snapshot(verified) == related


@pytest.mark.parametrize("endpoint", ["edit-xhr", "edit-html", "edit-photo", "visited", "rating"])
def test_csv_commits_between_home_read_and_write_cannot_be_overwritten_or_rolled_back(
    writer_engine: Engine, endpoint: str,
) -> None:
    applied_versions: list[int] = []
    with Session(writer_engine) as before:
        related = related_snapshot(before)

    def apply_two_csv_updates_before_home_write(
        connection: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        if not statement.startswith("UPDATE shops ") or applied_versions:
            return
        applied_versions.append(1)
        with Session(writer_engine) as other:
            assert other.connection() is not connection
            for version in (1, 2):
                apply_csv_update(other, parse_csv_update(csv_update(version), "concurrent.csv"))
                applied_versions.append(version + 1)

    event.listen(writer_engine, "before_cursor_execute", apply_two_csv_updates_before_home_write)
    try:
        with TestClient(writer_app(writer_engine)) as client:
            if endpoint.startswith("edit-"):
                payload = {
                    "shop_name": "My unsaved name", "area": "", "category": "カフェ",
                    "address": "My unsaved address", "memo": "My unsaved memo", "rating": "2",
                    "csrf_token": "writer-token", "expected_version": "1",
                }
                headers = {} if endpoint == "edit-html" else {"X-Requested-With": "XMLHttpRequest"}
                if endpoint == "edit-photo":
                    image_bytes = io.BytesIO()
                    with Image.new("RGB", (30, 30), "red") as photo:
                        photo.save(image_bytes, format="PNG")
                    response = client.post(
                        "/shop/1/edit", data=payload, headers=headers,
                        files={"photo": ("new.png", image_bytes.getvalue(), "image/png")},
                        follow_redirects=False,
                    )
                else:
                    response = client.post(
                        "/shop/1/edit", data=payload, headers=headers, follow_redirects=False,
                    )
            else:
                response = client.post(
                    f"/shop/1/{endpoint}", json={"rating": 2},
                    headers={"X-CSRF-Token": "writer-token", "X-Shop-Version": "1"},
                )
    finally:
        event.remove(writer_engine, "before_cursor_execute", apply_two_csv_updates_before_home_write)

    assert applied_versions == [1, 2, 3]
    assert response.status_code == 409, response.text
    assert response.headers["cache-control"] == "private, no-store"
    if endpoint == "edit-html":
        fields = FormValues()
        fields.feed(response.text)
        assert fields.inputs["shop_name"] == "My unsaved name"
        assert fields.inputs["address"] == "My unsaved address"
        assert "My unsaved memo" in response.text
    else:
        assert "更新されています" in response.json()["detail"]
    with Session(writer_engine) as verified:
        shop = verified.get(Shop, 1)
        assert shop is not None
        assert (shop.version, shop.shop_name, shop.area, shop.memo, shop.rating) == (
            3, "Saved shop", "銀座", "CSV update 2", 5,
        )
        assert shop.is_visited is False and shop.visited_at is None
        assert shop.image_key == "a" * 64
        assert related_snapshot(verified) == related
        untouched = verified.get(Shop, 2)
        assert untouched is not None and untouched.version == 1 and untouched.memo == "Untouched memo"
        with pytest.raises(ImportValidationFailure, match="更新済み"):
            apply_csv_update(verified, parse_csv_update(csv_update(2), "stale-retry.csv"))
        assert verified.get(Shop, 1).version == 3


@pytest.mark.parametrize("endpoint", ["edit", "visited", "rating"])
def test_home_write_failure_rolls_back_reserved_version_and_fields(
    writer_engine: Engine, endpoint: str,
) -> None:
    with Session(writer_engine) as db:
        db.execute(text(
            "CREATE TRIGGER fail_home_fields BEFORE UPDATE ON shops "
            "WHEN NEW.memo IS NOT OLD.memo OR NEW.rating IS NOT OLD.rating OR NEW.is_visited IS NOT OLD.is_visited "
            "BEGIN SELECT RAISE(ABORT, 'synthetic field write failure'); END"
        ))
        db.commit()
    with TestClient(writer_app(writer_engine)) as client:
        if endpoint == "edit":
            response = client.post("/shop/1/edit", data={
                "shop_name": "Changed name", "area": "銀座", "memo": "Changed memo",
                "csrf_token": "writer-token", "expected_version": "1",
            }, headers={"X-Requested-With": "XMLHttpRequest"})
        else:
            response = client.post(
                f"/shop/1/{endpoint}", json={"rating": 5},
                headers={"X-CSRF-Token": "writer-token", "X-Shop-Version": "1"},
            )
    assert response.status_code == 503
    with Session(writer_engine) as verified:
        shop = verified.get(Shop, 1)
        assert shop is not None
        assert (shop.version, shop.shop_name, shop.memo, shop.rating, shop.is_visited) == (
            1, "Saved shop", "Saved memo", 3, False,
        )

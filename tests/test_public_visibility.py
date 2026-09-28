from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Generator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import StreamingResponse

from db.models import Base, Message, MetadataReviewStatus, Shop, ShopMention
from web.routers.home import (
    _build_shop_query,
    _get_categories,
    _validate_optional_http_url,
    export_csv,
    home,
    shop_detail,
    shop_edit,
)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def request(path: str, *, admin: bool = False) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "session": {
                "authenticated": True,
                "admin_authenticated": admin,
                "csrf_token": "test-token",
            },
        }
    )


def add_mention(
    db: Session,
    shop: Shop,
    suffix: int,
    identity_status: str,
    metadata_status: str,
) -> ShopMention:
    mention = ShopMention(
        message=Message(message_id=f"3234567890123{suffix:04d}"),
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        extracted_area=shop.area,
        extracted_category=shop.category,
        resolution_status="resolved",
        review_status=identity_status,
        metadata_review_status=metadata_status,
        extraction_source="test",
    )
    db.add(mention)
    return mention


async def response_bytes(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


def test_public_queries_require_both_approvals_on_the_same_mention(db: Session) -> None:
    public_shop = Shop(shop_name="Public", area="銀座", category="割烹")
    metadata_pending = Shop(
        shop_name="Metadata Pending",
        area="新宿",
        category="ラーメン",
    )
    split_approval = Shop(
        shop_name="Split Approval",
        area="渋谷",
        category="カフェ・喫茶店",
    )
    add_mention(db, public_shop, 1, "approved", "approved")
    add_mention(db, metadata_pending, 2, "approved", "pending")
    add_mention(db, split_approval, 3, "approved", "pending")
    add_mention(db, split_approval, 4, "pending", "approved")
    db.commit()

    shops = _build_shop_query(db, None, None).all()
    assert [shop.id for shop in shops] == [public_shop.id]
    assert _get_categories(db) == ["割烹"]

    response = home(request("/"), db=db)
    assert response.context["total"] == 1
    assert response.context["grouped_areas"] == [("東京 / 中央区", [("銀座", 1)])]


def test_public_detail_and_csv_are_admin_only(
    db: Session,
) -> None:
    public_shop = Shop(shop_name="Public", area="銀座", category="割烹")
    private_shop = Shop(shop_name="Private", area="新宿", category="ラーメン")
    add_mention(db, public_shop, 5, "approved", "approved")
    add_mention(db, private_shop, 6, "approved", "pending")
    db.commit()

    with pytest.raises(HTTPException) as raised:
        shop_detail(private_shop.id, request(f"/shop/{private_shop.id}"), db=db)
    assert raised.value.status_code == 404

    admin_detail = shop_detail(
        private_shop.id,
        request(f"/shop/{private_shop.id}", admin=True),
        db=db,
    )
    assert admin_detail.context["shop"].id == private_shop.id

    response = export_csv(request("/export.csv"), db=db)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"

    admin_response = export_csv(request("/export.csv", admin=True), db=db)
    assert isinstance(admin_response, StreamingResponse)
    admin_rows = list(
        csv.DictReader(
            io.StringIO(
                asyncio.run(response_bytes(admin_response)).decode("utf-8-sig")
            )
        )
    )
    assert {row["shop.name"] for row in admin_rows} == {"Public", "Private"}


def test_clearing_shop_area_reopens_metadata_and_hides_shop(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    shop = Shop(shop_name="Public", area="銀座", category="割烹")
    mention = add_mention(db, shop, 7, "approved", "approved")
    db.commit()

    shop_edit(
        shop_id=shop.id,
        request=request(f"/shop/{shop.id}/edit", admin=True),
        shop_name=shop.shop_name,
        area=None,
        category=shop.category,
        url=None,
        address=None,
        phone=None,
        memo=None,
        rating=None,
        is_visited=None,
        visited_at=None,
        csrf_token="test-token",
        db=db,
    )

    assert shop.area is None
    assert mention.metadata_review_status == MetadataReviewStatus.PENDING.value
    assert mention.metadata_difference_type == "missing_area"
    assert mention.version == 2
    assert _build_shop_query(db, None, None).all() == []


def test_shop_edit_url_rejects_userinfo_without_echoing_it() -> None:
    secret = "do-not-echo"

    with pytest.raises(HTTPException) as raised:
        _validate_optional_http_url(
            f"https://user:{secret}@example.com/shop"
        )

    assert raised.value.status_code == 400
    assert secret not in str(raised.value.detail)

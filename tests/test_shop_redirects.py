from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse

from db.models import Base, Shop, ShopRedirect
from web.routers.home import shop_delete, shop_detail


def request_with_session(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "session": {
                "authenticated": True,
                "admin_authenticated": True,
                "csrf_token": "test-token",
            },
        }
    )


def test_old_shop_detail_redirects_to_merge_target() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        target = Shop(shop_name="Target")
        db.add(target)
        db.flush()
        db.add(ShopRedirect(source_shop_id=41, target_shop_id=target.id))
        db.commit()

        response = shop_detail(
            shop_id=41,
            request=request_with_session("/shop/41"),
            db=db,
        )

        assert isinstance(response, RedirectResponse)
        assert response.status_code == 308
        assert response.headers["location"] == f"/shop/{target.id}"
    finally:
        db.close()
        engine.dispose()


def test_merge_target_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        target = Shop(shop_name="Target")
        db.add(target)
        db.flush()
        db.add(ShopRedirect(source_shop_id=41, target_shop_id=target.id))
        db.commit()

        with pytest.raises(HTTPException) as raised:
            shop_delete(
                shop_id=target.id,
                request=request_with_session(f"/shop/{target.id}/delete"),
                csrf_token="test-token",
                db=db,
            )

        assert raised.value.status_code == 409
    finally:
        db.close()
        engine.dispose()

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.models import Shop
from services.import_service import collect_external_identities
from services.resolution import (
    CandidateIdentity,
    evaluate_identity,
    normalize_name,
    normalize_url_identity,
)
from web.area_groups import canonicalize_area


_SHOP_CREATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"meshi-archive:new-shop-creation").digest()[:8],
    "big",
    signed=True,
)

ShopCreationCollisionKind = Literal[
    "external_id",
    "canonical_url",
    "name_area",
    "strong_identity",
]
ShopCreationCollisionEvidence = Literal[
    "external_id",
    "canonical_url",
    "evidence_url",
    "name_area",
    "strong_identity",
]


@dataclass(frozen=True)
class ShopCreationCollision:
    kind: ShopCreationCollisionKind
    shop_id: int
    matched_by: ShopCreationCollisionEvidence


def lock_new_shop_creation(db: Session) -> None:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise RuntimeError(f"Unsupported database for shop creation lock: dialect={dialect}")
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _SHOP_CREATION_LOCK_KEY},
    )


def find_shop_creation_collision(
    db: Session,
    *,
    shop_name: str,
    branch_name: str | None,
    area: str | None,
    address: str | None,
    phone: str | None,
    canonical_url: str | None,
    evidence_url: str | None,
    external_source: str | None,
    external_id: str | None,
    exclude_shop_id: int | None = None,
) -> ShopCreationCollision | None:
    collisions = find_shop_creation_collisions(
        db,
        shop_name=shop_name,
        branch_name=branch_name,
        area=area,
        address=address,
        phone=phone,
        canonical_url=canonical_url,
        evidence_url=evidence_url,
        external_source=external_source,
        external_id=external_id,
        exclude_shop_id=exclude_shop_id,
    )
    return collisions[0] if collisions else None


def find_shop_creation_collisions(
    db: Session,
    *,
    shop_name: str,
    branch_name: str | None,
    area: str | None,
    address: str | None,
    phone: str | None,
    canonical_url: str | None,
    evidence_url: str | None,
    external_source: str | None,
    external_id: str | None,
    exclude_shop_id: int | None = None,
) -> tuple[ShopCreationCollision, ...]:
    normalized_name = normalize_name(shop_name)
    normalized_branch = normalize_name(branch_name or "")
    if normalized_branch and not normalized_name.endswith(normalized_branch):
        normalized_name = f"{normalized_name}{normalized_branch}"
    canonical_area = canonicalize_area(area)
    candidate_external_identities = collect_external_identities(
        external_source=external_source,
        external_id=external_id,
        urls=(canonical_url, evidence_url),
    )
    normalized_canonical_url = normalize_url_identity(canonical_url)
    normalized_evidence_url = normalize_url_identity(evidence_url)
    query = db.query(Shop)
    if exclude_shop_id is not None:
        query = query.filter(Shop.id != exclude_shop_id)
    shops = query.order_by(Shop.id.asc()).all()
    collisions: list[ShopCreationCollision] = []
    seen: set[tuple[ShopCreationCollisionKind, int, ShopCreationCollisionEvidence]] = set()

    def add_collision(
        kind: ShopCreationCollisionKind,
        shop_id: int,
        matched_by: ShopCreationCollisionEvidence,
    ) -> None:
        key = (kind, shop_id, matched_by)
        if key not in seen:
            seen.add(key)
            collisions.append(ShopCreationCollision(kind, shop_id, matched_by))

    for shop in shops:
        shop_external_identities = collect_external_identities(
            external_source=shop.external_source,
            external_id=shop.external_id,
            urls=(shop.canonical_url,),
        )
        if candidate_external_identities & shop_external_identities:
            add_collision("external_id", shop.id, "external_id")
    for shop in shops:
        normalized_shop_url = normalize_url_identity(shop.canonical_url)
        if normalized_shop_url is None:
            continue
        if normalized_canonical_url == normalized_shop_url:
            add_collision("canonical_url", shop.id, "canonical_url")
        if normalized_evidence_url == normalized_shop_url:
            add_collision("canonical_url", shop.id, "evidence_url")
    for shop in shops:
        existing_name = normalize_name(shop.shop_name)
        existing_branch = normalize_name(shop.branch_name or "")
        if existing_branch and not existing_name.endswith(existing_branch):
            existing_name = f"{existing_name}{existing_branch}"
        if (
            existing_name == normalized_name
            and canonicalize_area(shop.area) == canonical_area
        ):
            add_collision("name_area", shop.id, "name_area")
    candidate_identity = CandidateIdentity(
        name=f"{shop_name} {branch_name or ''}".strip(),
        area=canonical_area,
        address=address,
        phone=phone,
        external_source=external_source,
        external_id=external_id,
    )
    for shop in shops:
        existing_identity = CandidateIdentity(
            name=f"{shop.shop_name} {shop.branch_name or ''}".strip(),
            area=shop.area,
            address=shop.address,
            phone=shop.phone,
            external_source=shop.external_source,
            external_id=shop.external_id,
        )
        if evaluate_identity(existing_identity, candidate_identity).is_strong_match:
            add_collision("strong_identity", shop.id, "strong_identity")
    return tuple(collisions)

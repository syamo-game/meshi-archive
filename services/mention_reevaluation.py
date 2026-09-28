from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from bot.restaurant_extractor import is_known_category
from db.models import (
    CandidateProvenance,
    MetadataReviewStatus,
    ResolutionCandidate,
    ResolutionBasis,
    ResolutionMethod,
    ResolutionStatus,
    ReviewEvent,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
    utc_now,
)
from services.resolution import (
    CandidateIdentity,
    branches_conflict,
    evaluate_identity,
    extract_branch_token,
    normalize_address,
    normalize_external_identity,
    normalize_name,
    normalize_phone,
    normalize_url_identity,
    name_similarity,
)
from web.area_groups import area_is_municipality, canonicalize_area, is_known_area


@dataclass(frozen=True)
class AutomaticMentionResolution:
    mention_ids: tuple[int, ...]


@dataclass(frozen=True)
class ReviewGroupSummary:
    key: str
    reason: str
    mention_ids: tuple[int, ...]


def _normalized_name_with_branch(name: str, branch_name: str | None) -> str:
    normalized_name = normalize_name(name)
    normalized_branch = normalize_name(branch_name or extract_branch_token(name) or "")
    if normalized_branch and not normalized_name.endswith(normalized_branch):
        return f"{normalized_name}{normalized_branch}"
    return normalized_name


def _normalized_branch(name: str, branch_name: str | None) -> str:
    return normalize_name(branch_name or extract_branch_token(name) or "")


def _candidate_can_group(
    mention: ShopMention,
    candidate: ResolutionCandidate,
) -> bool:
    if candidate.provenance == CandidateProvenance.IMAGE.value:
        return False
    if (
        not candidate.is_verified
        and candidate.provenance != CandidateProvenance.WEB_SEARCH.value
    ):
        return False
    mention_name = (
        f"{mention.extracted_name} {mention.extracted_branch_name}"
        if mention.extracted_branch_name
        else mention.extracted_name
    )
    return bool(
        not branches_conflict(mention_name, candidate.name)
        and name_similarity(mention_name, candidate.name) >= 0.8
    )


def _candidates_have_conflicting_external_identity(
    candidates: tuple[ResolutionCandidate, ...],
) -> bool:
    external_ids_by_source: dict[str, set[str]] = {}
    for candidate in candidates:
        identity = normalize_external_identity(
            candidate.external_source,
            candidate.external_id,
        )
        if identity is None:
            continue
        source, external_id = identity
        external_ids_by_source.setdefault(source, set()).add(external_id)
    if any(
        len(external_ids) > 1
        for external_ids in external_ids_by_source.values()
    ):
        return True
    return False


def _candidates_have_conflicting_identity(
    candidates: tuple[ResolutionCandidate, ...],
) -> bool:
    if _candidates_have_conflicting_external_identity(candidates):
        return True
    phones = {
        phone
        for candidate in candidates
        if (phone := normalize_phone(candidate.phone)) is not None
    }
    if len(phones) > 1:
        return True
    addresses = {
        address
        for candidate in candidates
        if (address := normalize_address(candidate.address)) is not None
    }
    return len(addresses) > 1


def _candidate_areas(
    mention: ShopMention,
    *,
    allow_unverified: bool,
) -> frozenset[str]:
    mention_name = _normalized_name_with_branch(
        mention.extracted_name,
        mention.extracted_branch_name,
    )
    matching_candidates: list[ResolutionCandidate] = []
    for candidate in mention.candidates:
        if not candidate.is_verified and not (
            allow_unverified and _candidate_can_group(mention, candidate)
        ):
            continue
        if _normalized_name_with_branch(candidate.name, None) != mention_name:
            continue
        matching_candidates.append(candidate)
    candidates = tuple(matching_candidates)
    if _candidates_have_conflicting_identity(candidates):
        return frozenset()
    areas: set[str] = set()
    for candidate in candidates:
        area = canonicalize_area(candidate.area)
        if area is not None:
            areas.add(area)
    return frozenset(areas)


def _mention_exact_identity(
    mention: ShopMention,
    *,
    allow_unverified_area: bool = False,
) -> tuple[str, str, str] | None:
    name = _normalized_name_with_branch(
        mention.extracted_name,
        mention.extracted_branch_name,
    )
    if not name:
        return None
    extracted_area = canonicalize_area(mention.extracted_area)
    if extracted_area is None and (mention.extracted_area or "").strip():
        return None
    candidate_areas = _candidate_areas(
        mention,
        allow_unverified=allow_unverified_area,
    )
    if extracted_area is not None and candidate_areas - {extracted_area}:
        return None
    area = extracted_area
    if area is None and len(candidate_areas) == 1:
        area = next(iter(candidate_areas))
    if area is None or area_is_municipality(area):
        return None
    return (
        name,
        _normalized_branch(mention.extracted_name, mention.extracted_branch_name),
        area,
    )


def _shop_exact_identity(shop: Shop) -> tuple[str, str, str] | None:
    area = canonicalize_area(shop.area)
    name = _normalized_name_with_branch(shop.shop_name, shop.branch_name)
    if not name or area is None or area_is_municipality(area):
        return None
    return (
        name,
        _normalized_branch(shop.shop_name, shop.branch_name),
        area,
    )


def _shop_has_approved_exact_identity(
    shop: Shop,
    exact_identity: tuple[str, str, str],
) -> bool:
    if _shop_exact_identity(shop) == exact_identity:
        return True
    return any(
        mention.review_status == ReviewStatus.APPROVED.value
        and _mention_exact_identity(mention) == exact_identity
        for mention in shop.mentions
    )


def _shop_candidate_identity(shop: Shop) -> CandidateIdentity:
    return CandidateIdentity(
        name=f"{shop.shop_name} {shop.branch_name or ''}".strip(),
        area=shop.area,
        address=shop.address,
        phone=shop.phone,
        canonical_url=shop.canonical_url,
        external_source=shop.external_source,
        external_id=shop.external_id,
    )


def _resolution_candidate_identity(candidate: ResolutionCandidate) -> CandidateIdentity:
    return CandidateIdentity(
        name=candidate.name,
        area=candidate.area,
        category=candidate.category,
        address=candidate.address,
        phone=candidate.phone,
        canonical_url=candidate.canonical_url,
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        evidence_url=candidate.evidence_url,
    )


def _candidate_can_affect_resolution(candidate: ResolutionCandidate) -> bool:
    return bool(candidate.is_verified or candidate.is_strong_match)


def _mention_conflicts_with_shop(mention: ShopMention, shop: Shop) -> bool:
    mention_branch = _normalized_branch(
        mention.extracted_name,
        mention.extracted_branch_name,
    )
    shop_branch = _normalized_branch(shop.shop_name, shop.branch_name)
    if mention_branch and shop_branch and mention_branch != shop_branch:
        return True
    mention_area = canonicalize_area(mention.extracted_area)
    shop_area = canonicalize_area(shop.area)
    if mention_area is not None and shop_area is not None and mention_area != shop_area:
        return True

    shop_identity = _shop_candidate_identity(shop)
    for candidate in mention.candidates:
        if not _candidate_can_affect_resolution(candidate):
            continue
        candidate_area = canonicalize_area(candidate.area)
        if (
            candidate_area is not None
            and shop_area is not None
            and candidate_area != shop_area
        ):
            return True
        outcome = evaluate_identity(
            shop_identity,
            _resolution_candidate_identity(candidate),
        )
        if "branch" in outcome.conflicting_fields:
            return True
        if "external_id" in outcome.conflicting_fields:
            return True
        if "address" in outcome.conflicting_fields:
            return True
        if "phone" in outcome.conflicting_fields:
            return True
    return False


def _candidate_strong_shop_ids(
    mention: ShopMention,
    approved_shops: tuple[Shop, ...],
) -> frozenset[int]:
    if mention.extracted_branch_name:
        mention_name = (
            f"{mention.extracted_name} {mention.extracted_branch_name}"
        )
    else:
        mention_name = mention.extracted_name
    matching_ids: set[int] = set()
    for candidate in mention.candidates:
        if not candidate.is_verified:
            continue
        if branches_conflict(mention_name, candidate.name):
            continue
        candidate_identity = _resolution_candidate_identity(candidate)
        for shop in approved_shops:
            if _mention_conflicts_with_shop(mention, shop):
                continue
            outcome = evaluate_identity(
                _shop_candidate_identity(shop),
                candidate_identity,
            )
            if outcome.is_strong_match and not outcome.conflicting_fields:
                matching_ids.add(shop.id)
    return frozenset(matching_ids)


def _candidate_external_shop_ids(
    mention: ShopMention,
    approved_shops: tuple[Shop, ...],
) -> frozenset[int]:
    candidate_identities = {
        identity
        for candidate in mention.candidates
        if candidate.is_verified
        and (
            identity := normalize_external_identity(
                candidate.external_source,
                candidate.external_id,
            )
        )
        is not None
    }
    if not candidate_identities:
        return frozenset()
    return frozenset(
        shop.id
        for shop in approved_shops
        if normalize_external_identity(shop.external_source, shop.external_id)
        in candidate_identities
    )


def _approved_shops(db: Session) -> tuple[Shop, ...]:
    return tuple(
        db.query(Shop)
        .filter(
            Shop.mentions.any(
                ShopMention.review_status == ReviewStatus.APPROVED.value
            )
        )
        .options(
            selectinload(Shop.mentions).selectinload(ShopMention.candidates)
        )
        .execution_options(populate_existing=True)
        .order_by(Shop.id.asc())
        .all()
    )


def _reusable_resolution_basis(
    mention: ShopMention,
    target_shop: Shop,
    approved_shops: tuple[Shop, ...],
) -> ResolutionBasis | None:
    if mention.shop_id not in {None, target_shop.id}:
        return None
    verified_candidates = tuple(
        candidate for candidate in mention.candidates if candidate.is_verified
    )
    if _candidates_have_conflicting_identity(verified_candidates):
        return None
    if _mention_conflicts_with_shop(mention, target_shop):
        return None
    candidate_external_shop_ids = _candidate_external_shop_ids(
        mention,
        approved_shops,
    )
    if candidate_external_shop_ids - {target_shop.id}:
        return None

    exact_identity = _mention_exact_identity(mention)
    if exact_identity is not None:
        exact_shop_ids = {
            shop.id
            for shop in approved_shops
            if _shop_has_approved_exact_identity(shop, exact_identity)
        }
        strong_shop_ids = _candidate_strong_shop_ids(mention, approved_shops)
        if exact_shop_ids == {target_shop.id} and not (
            strong_shop_ids - {target_shop.id}
        ):
            return ResolutionBasis.EXISTING_SHOP

    strong_shop_ids = _candidate_strong_shop_ids(mention, approved_shops)
    if strong_shop_ids == {target_shop.id}:
        return ResolutionBasis.VERIFIED_CANDIDATE
    return None


def _pending_scan_statement(source_mention_id: int) -> Select[tuple[ShopMention]]:
    return (
        select(ShopMention)
        .where(
            ShopMention.id != source_mention_id,
            ShopMention.review_status == ReviewStatus.PENDING.value,
        )
        .options(selectinload(ShopMention.candidates))
        .order_by(ShopMention.id.asc())
    )


def _pending_lock_statement(
    mention_ids: tuple[int, ...],
    *,
    dialect_name: str,
) -> Select[tuple[ShopMention]]:
    statement = (
        select(ShopMention)
        .where(
            ShopMention.id.in_(mention_ids),
            ShopMention.review_status == ReviewStatus.PENDING.value,
        )
        .options(selectinload(ShopMention.candidates))
        .order_by(ShopMention.id.asc())
        .execution_options(populate_existing=True)
    )
    if dialect_name == "postgresql":
        return statement.with_for_update(skip_locked=True)
    if dialect_name == "sqlite":
        return statement.with_for_update()
    raise RuntimeError(
        f"Unsupported database for mention reevaluation lock: dialect={dialect_name}"
    )


def auto_resolve_pending_mentions(
    db: Session,
    *,
    source_mention: ShopMention,
    target_shop: Shop,
) -> AutomaticMentionResolution:
    approved_shops = _approved_shops(db)
    pending_mentions = tuple(
        db.scalars(_pending_scan_statement(source_mention.id)).all()
    )
    candidate_ids = tuple(
        mention.id
        for mention in pending_mentions
        if _reusable_resolution_basis(mention, target_shop, approved_shops)
        is not None
    )
    if not candidate_ids:
        return AutomaticMentionResolution(mention_ids=())

    dialect_name = db.get_bind().dialect.name
    locked_mentions = tuple(
        db.scalars(
            _pending_lock_statement(
                candidate_ids,
                dialect_name=dialect_name,
            )
        ).all()
    )
    current_approved_shops = _approved_shops(db)
    resolved_ids: list[int] = []
    for mention in locked_mentions:
        basis = _reusable_resolution_basis(
            mention,
            target_shop,
            current_approved_shops,
        )
        if basis is None:
            continue
        previous_shop_id = mention.shop_id
        mention.shop = target_shop
        mention.resolution_status = ResolutionStatus.RESOLVED.value
        mention.review_status = ReviewStatus.APPROVED.value
        mention.resolution_method = ResolutionMethod.AUTOMATIC.value
        mention.resolution_basis = basis.value
        mention.reused_from_mention_id = source_mention.id
        mention.reviewed_at = utc_now()
        if is_known_area(target_shop.area) and is_known_category(
            target_shop.category
        ):
            mention.metadata_review_status = MetadataReviewStatus.APPROVED.value
            mention.metadata_difference_type = None
            mention.metadata_reviewed_at = utc_now()
        elif mention.metadata_review_status != MetadataReviewStatus.DEFERRED.value:
            mention.metadata_review_status = MetadataReviewStatus.PENDING.value
            mention.metadata_reviewed_at = None
            if not target_shop.area:
                mention.metadata_difference_type = "missing_area"
            elif not is_known_area(target_shop.area):
                mention.metadata_difference_type = "unknown_area"
            elif not target_shop.category:
                mention.metadata_difference_type = "missing_category"
            else:
                mention.metadata_difference_type = "unknown_category"
        mention.version += 1
        db.add(
            ReviewEvent(
                mention=mention,
                scope=ReviewScope.IDENTITY.value,
                action="auto_reuse_approved_identity",
                previous_shop_id=previous_shop_id,
                selected_shop_id=target_shop.id,
                note=(
                    f"basis={basis.value}; "
                    f"reused_from_mention_id={source_mention.id}"
                ),
            )
        )
        resolved_ids.append(mention.id)
    return AutomaticMentionResolution(mention_ids=tuple(resolved_ids))


def _hashed_group_key(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:24]}"


def _candidate_group_key(
    mention: ShopMention,
) -> tuple[str, str] | None:
    groupable = [
        candidate
        for candidate in mention.candidates
        if _candidate_can_group(mention, candidate)
    ]
    if _candidates_have_conflicting_external_identity(tuple(groupable)):
        return None
    external_ids = {
        identity
        for candidate in groupable
        if (
            identity := normalize_external_identity(
                candidate.external_source,
                candidate.external_id,
            )
        )
        is not None
    }
    if len(external_ids) == 1:
        source, external_id = next(iter(external_ids))
        return (
            _hashed_group_key("external_id", f"{source}:{external_id}"),
            "候補の外部IDが一致",
        )

    canonical_urls: set[str] = set()
    for candidate in groupable:
        if candidate.canonical_url is None:
            continue
        normalized = normalize_url_identity(candidate.canonical_url)
        if normalized is not None:
            canonical_urls.add(normalized)
    if len(canonical_urls) == 1:
        canonical_url = next(iter(canonical_urls))
        return (
            _hashed_group_key("canonical_url", canonical_url),
            "候補の店舗URLが一致",
        )

    phone_names = {
        (phone, normalize_name(candidate.name))
        for candidate in groupable
        if (phone := normalize_phone(candidate.phone)) is not None
    }
    if len(phone_names) == 1:
        phone, name = next(iter(phone_names))
        return (
            _hashed_group_key("phone_name", f"{phone}:{name}"),
            "候補の電話番号と店名が一致",
        )

    address_names = {
        (address, normalize_name(candidate.name))
        for candidate in groupable
        if (address := normalize_address(candidate.address)) is not None
    }
    if len(address_names) == 1:
        address, name = next(iter(address_names))
        return (
            _hashed_group_key("address_name", f"{address}:{name}"),
            "候補の住所と店名が一致",
        )
    return None


def _review_group_keys(mention: ShopMention) -> tuple[tuple[str, str], ...]:
    keys: list[tuple[str, str]] = []
    candidate_key = _candidate_group_key(mention)
    if candidate_key is not None:
        keys.append(candidate_key)
    exact_identity = _mention_exact_identity(
        mention,
        allow_unverified_area=True,
    )
    if exact_identity is not None:
        name, branch, area = exact_identity
        keys.append(
            (
                _hashed_group_key("mention_identity", f"{name}:{branch}:{area}"),
                "抽出した店名・支店名・エリアが一致",
            )
        )
    return tuple(keys)


def review_group_key(mention: ShopMention) -> tuple[str, str] | None:
    keys = _review_group_keys(mention)
    return keys[0] if keys else None


def build_open_review_group_index(
    db: Session,
) -> dict[int, ReviewGroupSummary]:
    open_mentions = (
        db.query(ShopMention)
        .filter(
            ShopMention.review_status.in_(
                (ReviewStatus.PENDING.value, ReviewStatus.DEFERRED.value)
            )
        )
        .options(selectinload(ShopMention.candidates))
        .order_by(ShopMention.id.asc())
        .all()
    )
    groups: dict[str, tuple[str, list[int]]] = {}
    for mention in open_mentions:
        for key, reason in _review_group_keys(mention):
            if key not in groups:
                groups[key] = (reason, [])
            groups[key][1].append(mention.id)

    index: dict[int, ReviewGroupSummary] = {}
    priorities = {
        "external_id": 0,
        "canonical_url": 1,
        "phone_name": 2,
        "address_name": 3,
        "mention_identity": 4,
    }
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            len(item[1][1]) < 2,
            priorities.get(item[0].partition(":")[0], len(priorities)),
            -len(item[1][1]),
            item[0],
        ),
    )
    assigned_ids: set[int] = set()
    for key, (reason, member_ids) in ordered_groups:
        available_ids = tuple(
            mention_id
            for mention_id in member_ids
            if mention_id not in assigned_ids
        )
        if not available_ids:
            continue
        summary = ReviewGroupSummary(
            key=key,
            reason=reason,
            mention_ids=available_ids,
        )
        for mention_id in available_ids:
            index[mention_id] = summary
            assigned_ids.add(mention_id)
    return index

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Literal

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from db.models import (
    ReviewEvent,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
    ShopRedirect,
)
from services.import_service import (
    collect_external_identities,
    external_identities_conflict,
)
from services.resolution import (
    branches_conflict,
    extract_branch_token,
    normalize_address,
    normalize_area,
    normalize_name,
    normalize_phone,
    normalize_url_identity,
)
from services.shop_creation_lock import lock_new_shop_creation
from services.merge_history import snapshot_merge


@dataclass(frozen=True)
class DuplicateEvidence:
    phone_match: bool
    address_match: bool


@dataclass(frozen=True)
class DuplicateShadowGroup:
    normalized_name: str
    normalized_branch: str
    normalized_area: str
    shop_ids: tuple[int, ...]
    evidence: DuplicateEvidence
    identity_conflicts: tuple[str, ...]
    user_data_conflicts: tuple[str, ...]
    would_auto_merge: bool


@dataclass(frozen=True)
class DuplicateShadowReport:
    groups: tuple[DuplicateShadowGroup, ...]

    @property
    def candidate_shop_count(self) -> int:
        return sum(len(group.shop_ids) for group in self.groups)

    @property
    def auto_merge_group_count(self) -> int:
        return sum(1 for group in self.groups if group.would_auto_merge)


@dataclass(frozen=True)
class SafeDuplicateMergeGroup:
    shop_ids: tuple[int, ...]
    keep_shop_id: int
    evidence: tuple[str, ...]
    is_safe: bool
    reason: str
    identity_conflicts: tuple[str, ...]
    user_data_conflicts: tuple[str, ...]


@dataclass(frozen=True)
class SafeDuplicateMergePlan:
    groups: tuple[SafeDuplicateMergeGroup, ...]

    @property
    def safe_group_count(self) -> int:
        return sum(1 for group in self.groups if group.is_safe)

    @property
    def safe_shop_count(self) -> int:
        return sum(len(group.shop_ids) for group in self.groups if group.is_safe)


SafeDuplicateMergeAction = Literal["merged", "skipped"]


@dataclass(frozen=True)
class SafeDuplicateMergeResultGroup:
    shop_ids: tuple[int, ...]
    keep_shop_id: int
    evidence: tuple[str, ...]
    action: SafeDuplicateMergeAction
    reason: str
    merged_shop_count: int
    moved_mention_count: int
    complemented_fields: tuple[str, ...]


@dataclass(frozen=True)
class SafeDuplicateMergeResult:
    groups: tuple[SafeDuplicateMergeResultGroup, ...]

    @property
    def merged_group_count(self) -> int:
        return sum(1 for group in self.groups if group.action == "merged")

    @property
    def merged_shop_count(self) -> int:
        return sum(group.merged_shop_count for group in self.groups)

    @property
    def moved_mention_count(self) -> int:
        return sum(group.moved_mention_count for group in self.groups)


def _normalized_shop_name(shop: Shop) -> tuple[str, str]:
    branch = normalize_name(shop.branch_name or extract_branch_token(shop.shop_name) or "")
    name = normalize_name(shop.shop_name)
    if branch and not name.endswith(branch):
        name = f"{name}{branch}"
    return name, branch


def _all_present_and_equal(values: tuple[str | None, ...]) -> bool:
    normalized = tuple(value for value in values if value)
    return len(normalized) == len(values) and len(set(normalized)) == 1


def _conflicting_fields(shops: tuple[Shop, ...]) -> tuple[str, ...]:
    fields: tuple[tuple[str, tuple[object, ...]], ...] = (
        ("external_identity", tuple(
            (shop.external_source, shop.external_id)
            if shop.external_source and shop.external_id
            else None
            for shop in shops
        )),
        ("phone", tuple(normalize_phone(shop.phone) for shop in shops)),
        ("address", tuple(normalize_address(shop.address) or None for shop in shops)),
    )
    return tuple(
        name
        for name, values in fields
        if len({value for value in values if value is not None}) > 1
    )


def _user_data_conflicts(shops: tuple[Shop, ...]) -> tuple[str, ...]:
    fields: tuple[tuple[str, tuple[object, ...]], ...] = (
        ("is_visited", tuple(shop.is_visited for shop in shops)),
        ("visited_at", tuple(shop.visited_at for shop in shops)),
        ("rating", tuple(shop.rating for shop in shops)),
        ("memo", tuple(shop.memo for shop in shops)),
        ("image_key", tuple(shop.image_key for shop in shops)),
    )
    conflicts: list[str] = []
    for name, values in fields:
        populated = {value for value in values if value is not None}
        if name == "is_visited":
            populated = set(values)
        if len(populated) > 1:
            conflicts.append(name)
    return tuple(conflicts)


def _safe_user_data_conflicts(shops: tuple[Shop, ...]) -> tuple[str, ...]:
    conflicts = list(_user_data_conflicts(shops))
    if any(not shop.is_visited and shop.visited_at is not None for shop in shops):
        conflicts.append("visit_state")
    return tuple(conflicts)


def _shop_name_with_branch(shop: Shop) -> str:
    branch = shop.branch_name or ""
    normalized_name = normalize_name(shop.shop_name)
    normalized_branch = normalize_name(branch)
    if not normalized_branch or normalized_name.endswith(normalized_branch):
        return shop.shop_name
    return f"{shop.shop_name} {branch}"


def _names_or_branches_conflict(shops: tuple[Shop, ...]) -> tuple[str, ...]:
    conflicts: set[str] = set()
    for left, right in combinations(shops, 2):
        left_branch = normalize_name(left.branch_name or extract_branch_token(left.shop_name) or "")
        right_branch = normalize_name(
            right.branch_name or extract_branch_token(right.shop_name) or ""
        )
        if left_branch and right_branch and left_branch != right_branch:
            conflicts.add("branch_name")
        left_name = _shop_name_with_branch(left)
        right_name = _shop_name_with_branch(right)
        if branches_conflict(left_name, right_name):
            conflicts.add("branch_name")
        normalized_left = normalize_name(left_name)
        normalized_right = normalize_name(right_name)
        shorter_length = min(len(normalized_left), len(normalized_right))
        if (
            not normalized_left
            or not normalized_right
            or shorter_length == 0
            or not (
                normalized_left == normalized_right
                or (
                    shorter_length >= 4
                    and (
                        normalized_left in normalized_right
                        or normalized_right in normalized_left
                    )
                )
            )
        ):
            conflicts.add("shop_name")
    return tuple(sorted(conflicts))


def _normalized_values_conflict(
    values: tuple[str | None, ...],
    *,
    normalizer: Callable[[str], str | None],
) -> bool:
    normalized_values: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = normalizer(value)
        if normalized:
            normalized_values.add(normalized)
    return len(normalized_values) > 1


def _safe_identity_conflicts(shops: tuple[Shop, ...]) -> tuple[str, ...]:
    conflicts = set(_names_or_branches_conflict(shops))
    external_identity_sets = tuple(_shop_external_identities(shop) for shop in shops)
    combined_external_identities: frozenset[tuple[str, str]] = frozenset().union(
        *external_identity_sets
    )
    explicit_external_identities: frozenset[tuple[str, str]] = frozenset().union(
        *(
            collect_external_identities(
                external_source=shop.external_source,
                external_id=shop.external_id,
            )
            for shop in shops
        )
    )
    if (
        any(external_identities_conflict(identities) for identities in external_identity_sets)
        or external_identities_conflict(combined_external_identities)
        or len(explicit_external_identities) > 1
    ):
        conflicts.add("external_identity")
    field_values: tuple[
        tuple[str, tuple[str | None, ...], Callable[[str], str | None]], ...
    ] = (
        ("area", tuple(shop.area for shop in shops), normalize_area),
        ("category", tuple(shop.category for shop in shops), normalize_name),
        ("address", tuple(shop.address for shop in shops), normalize_address),
        ("phone", tuple(shop.phone for shop in shops), normalize_phone),
        (
            "canonical_url",
            tuple(shop.canonical_url for shop in shops),
            normalize_url_identity,
        ),
    )
    for field_name, values, normalizer in field_values:
        if _normalized_values_conflict(values, normalizer=normalizer):
            conflicts.add(field_name)
    return tuple(sorted(conflicts))


def _phone_address_branch_presence_conflict(shops: tuple[Shop, ...]) -> bool:
    for left, right in combinations(shops, 2):
        left_phone = normalize_phone(left.phone)
        right_phone = normalize_phone(right.phone)
        left_address = normalize_address(left.address)
        right_address = normalize_address(right.address)
        if (
            not left_phone
            or left_phone != right_phone
            or not left_address
            or left_address != right_address
        ):
            continue
        left_branch = normalize_name(
            left.branch_name or extract_branch_token(left.shop_name) or ""
        )
        right_branch = normalize_name(
            right.branch_name or extract_branch_token(right.shop_name) or ""
        )
        if (
            bool(left_branch) != bool(right_branch)
            and not (_shop_external_identities(left) & _shop_external_identities(right))
        ):
            return True
    return False


def _approved_shops(db: Session) -> tuple[Shop, ...]:
    return tuple(
        db.query(Shop)
        .filter(Shop.mentions.any(ShopMention.review_status == ReviewStatus.APPROVED.value))
        .populate_existing()
        .order_by(Shop.id.asc())
        .all()
    )


def _shop_external_identities(shop: Shop) -> frozenset[tuple[str, str]]:
    return collect_external_identities(
        external_source=shop.external_source,
        external_id=shop.external_id,
        urls=(shop.canonical_url,),
    )


def _candidate_components(
    shops: tuple[Shop, ...],
) -> tuple[tuple[tuple[Shop, ...], tuple[str, ...]], ...]:
    parents = {shop.id: shop.id for shop in shops}
    evidence_by_edge: dict[tuple[int, int], set[str]] = {}

    def find(shop_id: int) -> int:
        parent = parents[shop_id]
        while parent != parents[parent]:
            parent = parents[parent]
        current = shop_id
        while parents[current] != parent:
            next_id = parents[current]
            parents[current] = parent
            current = next_id
        return parent

    def union(left_id: int, right_id: int) -> None:
        left_root = find(left_id)
        right_root = find(right_id)
        if left_root == right_root:
            return
        parents[max(left_root, right_root)] = min(left_root, right_root)

    external_groups: dict[tuple[str, str], list[Shop]] = {}
    phone_address_groups: dict[tuple[str, str], list[Shop]] = {}
    for shop in shops:
        for external_identity in _shop_external_identities(shop):
            external_groups.setdefault(external_identity, []).append(shop)
        phone = normalize_phone(shop.phone)
        address = normalize_address(shop.address)
        if phone and address:
            phone_address_groups.setdefault((phone, address), []).append(shop)

    evidence_groups: tuple[tuple[str, dict[object, list[Shop]]], ...] = (
        ("external_id", external_groups),
        ("phone_address", phone_address_groups),
    )
    for evidence_name, grouped_shops in evidence_groups:
        for members in grouped_shops.values():
            if len(members) < 2:
                continue
            for left, right in combinations(sorted(members, key=lambda shop: shop.id), 2):
                edge = (min(left.id, right.id), max(left.id, right.id))
                evidence_by_edge.setdefault(edge, set()).add(evidence_name)
                union(left.id, right.id)

    shops_by_root: dict[int, list[Shop]] = {}
    for shop in shops:
        root = find(shop.id)
        shops_by_root.setdefault(root, []).append(shop)

    components: list[tuple[tuple[Shop, ...], tuple[str, ...]]] = []
    for members in shops_by_root.values():
        if len(members) < 2:
            continue
        member_ids = {shop.id for shop in members}
        evidence = {
            item
            for edge, edge_evidence in evidence_by_edge.items()
            if edge[0] in member_ids and edge[1] in member_ids
            for item in edge_evidence
        }
        components.append(
            (
                tuple(sorted(members, key=lambda shop: shop.id)),
                tuple(sorted(evidence)),
            )
        )
    return tuple(sorted(components, key=lambda item: item[0][0].id))


def _build_safe_plan_from_shops(
    db: Session,
    shops: tuple[Shop, ...],
) -> SafeDuplicateMergePlan:
    groups: list[SafeDuplicateMergeGroup] = []
    for members, evidence in _candidate_components(shops):
        identity_conflict_set = set(_safe_identity_conflicts(members))
        if _phone_address_branch_presence_conflict(members):
            identity_conflict_set.add("branch_name")
        identity_conflicts = tuple(sorted(identity_conflict_set))
        user_data_conflicts = _safe_user_data_conflicts(members)
        redirect_conflict = _redirect_conflict_reason(db, members)
        if identity_conflicts:
            reason = f"identity_conflict:{','.join(identity_conflicts)}"
        elif user_data_conflicts:
            reason = f"user_data_conflict:{','.join(user_data_conflicts)}"
        elif redirect_conflict is not None:
            reason = redirect_conflict
        else:
            reason = f"safe_evidence:{','.join(evidence)}"
        groups.append(
            SafeDuplicateMergeGroup(
                shop_ids=tuple(shop.id for shop in members),
                keep_shop_id=members[0].id,
                evidence=evidence,
                is_safe=(
                    not identity_conflicts
                    and not user_data_conflicts
                    and redirect_conflict is None
                ),
                reason=reason,
                identity_conflicts=identity_conflicts,
                user_data_conflicts=user_data_conflicts,
            )
        )
    return SafeDuplicateMergePlan(groups=tuple(groups))


def _redirect_conflict_reason(
    db: Session,
    shops: tuple[Shop, ...],
) -> str | None:
    shop_ids = tuple(shop.id for shop in shops)
    keep_shop_id = shop_ids[0]
    losing_ids = set(shop_ids[1:])
    redirects = (
        db.query(ShopRedirect)
        .filter(
            or_(
                ShopRedirect.source_shop_id.in_(shop_ids),
                ShopRedirect.target_shop_id.in_(shop_ids),
            )
        )
        .populate_existing()
        .all()
    )
    if any(
        redirect.source_shop_id == keep_shop_id
        and redirect.target_shop_id in losing_ids
        for redirect in redirects
    ):
        return "redirect_conflict:keeper_would_redirect_to_itself"
    live_shop_ids = {
        shop_id
        for (shop_id,) in db.query(Shop.id)
        .filter(Shop.id.in_(tuple(redirect.source_shop_id for redirect in redirects)))
        .all()
    }
    if any(
        redirect.source_shop_id in live_shop_ids
        and redirect.source_shop_id not in losing_ids
        and redirect.target_shop_id in losing_ids
        for redirect in redirects
    ):
        return "redirect_conflict:live_shop_points_to_losing_shop"
    return None


def build_safe_duplicate_merge_plan(db: Session) -> SafeDuplicateMergePlan:
    return _build_safe_plan_from_shops(db, _approved_shops(db))


def _first_present(values: tuple[str | None, ...]) -> str | None:
    return next((value for value in values if value), None)


def _complement_keeper(
    keeper: Shop,
    shops: tuple[Shop, ...],
    *,
    moved_external_identity: tuple[str, str] | None,
) -> tuple[str, ...]:
    complemented: list[str] = []

    if not keeper.branch_name:
        value = _first_present(tuple(shop.branch_name for shop in shops))
        if value:
            keeper.branch_name = value
            complemented.append("branch_name")
    if not keeper.area:
        value = _first_present(tuple(shop.area for shop in shops))
        if value:
            keeper.area = value
            complemented.append("area")
    if not keeper.category:
        value = _first_present(tuple(shop.category for shop in shops))
        if value:
            keeper.category = value
            complemented.append("category")
    if not keeper.address:
        value = _first_present(tuple(shop.address for shop in shops))
        if value:
            keeper.address = value
            complemented.append("address")
    if not keeper.phone:
        value = _first_present(tuple(shop.phone for shop in shops))
        if value:
            keeper.phone = value
            complemented.append("phone")
    if not keeper.canonical_url:
        value = _first_present(tuple(shop.canonical_url for shop in shops))
        if value:
            keeper.canonical_url = value
            complemented.append("canonical_url")
    if keeper.image_key is None:
        value = _first_present(tuple(shop.image_key for shop in shops))
        if value:
            keeper.image_key = value
            complemented.append("image_key")
    if not keeper.external_source and not keeper.external_id:
        external_identity = moved_external_identity or next(
            (
                (shop.external_source, shop.external_id)
                for shop in shops
                if shop.external_source and shop.external_id
            ),
            None,
        )
        if external_identity is not None:
            keeper.external_source, keeper.external_id = external_identity
            complemented.append("external_identity")
    if keeper.is_visited and keeper.visited_at is None:
        visited_at = next((shop.visited_at for shop in shops if shop.visited_at), None)
        if visited_at is not None:
            keeper.visited_at = visited_at
            complemented.append("visited_at")
    if keeper.rating is None:
        rating = next((shop.rating for shop in shops if shop.rating is not None), None)
        if rating is not None:
            keeper.rating = rating
            complemented.append("rating")
    if keeper.memo is None:
        memo = next((shop.memo for shop in shops if shop.memo is not None), None)
        if memo is not None:
            keeper.memo = memo
            complemented.append("memo")

    keeper.version += 1
    return tuple(complemented)


def _lock_candidate_rows(
    db: Session,
    candidate_shop_ids: tuple[int, ...],
) -> tuple[Shop, ...]:
    dialect = db.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(
            f"Unsupported database for safe duplicate merge: dialect={dialect}"
        )
    shop_query = (
        db.query(Shop)
        .filter(Shop.id.in_(candidate_shop_ids))
        .populate_existing()
        .order_by(Shop.id.asc())
    )
    redirect_query = (
        db.query(ShopRedirect)
        .filter(
            or_(
                ShopRedirect.source_shop_id.in_(candidate_shop_ids),
                ShopRedirect.target_shop_id.in_(candidate_shop_ids),
            )
        )
        .populate_existing()
        .order_by(ShopRedirect.source_shop_id.asc())
    )
    if dialect == "postgresql":
        shops = tuple(shop_query.with_for_update().all())
        redirect_query.with_for_update().all()
        return shops
    redirect_query.all()
    return tuple(shop_query.all())


def _group_mentions_for_update(
    db: Session,
    shop_ids: tuple[int, ...],
) -> tuple[ShopMention, ...]:
    query = (
        db.query(ShopMention)
        .filter(ShopMention.shop_id.in_(shop_ids))
        .populate_existing()
        .order_by(ShopMention.id.asc())
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return tuple(query.all())


def _flatten_redirects(
    db: Session,
    *,
    keeper: Shop,
    losing_shop_ids: tuple[int, ...],
) -> None:
    incoming = (
        db.query(ShopRedirect)
        .filter(ShopRedirect.target_shop_id.in_(losing_shop_ids))
        .populate_existing()
        .order_by(ShopRedirect.source_shop_id.asc())
        .all()
    )
    for redirect in incoming:
        redirect.target = keeper
        redirect.reason = "safe_duplicate_merge"

    for losing_shop_id in losing_shop_ids:
        redirect = db.get(ShopRedirect, losing_shop_id)
        if redirect is None:
            db.add(
                ShopRedirect(
                    source_shop_id=losing_shop_id,
                    target=keeper,
                    reason="safe_duplicate_merge",
                )
            )
            continue
        redirect.target = keeper
        redirect.reason = "safe_duplicate_merge"


def _merge_safe_group(
    db: Session,
    group: SafeDuplicateMergeGroup,
    shops_by_id: dict[int, Shop],
) -> SafeDuplicateMergeResultGroup:
    shops = tuple(shops_by_id[shop_id] for shop_id in group.shop_ids)
    keeper = shops[0]
    losing_shops = shops[1:]
    losing_shop_ids = tuple(shop.id for shop in losing_shops)
    group_mentions = _group_mentions_for_update(db, group.shop_ids)
    approved_shop_ids = {
        mention.shop_id for mention in group_mentions
        if mention.review_status == ReviewStatus.APPROVED.value
    }
    if approved_shop_ids != set(group.shop_ids):
        return SafeDuplicateMergeResultGroup(
            shop_ids=group.shop_ids,
            keep_shop_id=group.keep_shop_id,
            evidence=group.evidence,
            action="skipped",
            reason="approved_mentions_changed",
            merged_shop_count=0,
            moved_mention_count=0,
            complemented_fields=(),
        )
    mentions = tuple(mention for mention in group_mentions if mention.shop_id in losing_shop_ids)
    note = snapshot_merge(
        shops,
        mentions,
        reason=f"safe duplicate merge; evidence={','.join(group.evidence)}",
    )
    moved_external_identity = None
    if not keeper.external_source and not keeper.external_id:
        moved_external_identity = next(
            (
                (shop.external_source, shop.external_id)
                for shop in losing_shops
                if shop.external_source and shop.external_id
            ),
            None,
        )
        if moved_external_identity is not None:
            for losing_shop in losing_shops:
                losing_shop.external_source = None
                losing_shop.external_id = None
            db.flush()
    complemented_fields = _complement_keeper(
        keeper,
        shops,
        moved_external_identity=moved_external_identity,
    )
    for mention in mentions:
        previous_shop_id = mention.shop_id
        mention.shop = keeper
        mention.version += 1
        db.add(
            ReviewEvent(
                mention_id=mention.id,
                scope=ReviewScope.IDENTITY.value,
                action="automatic_merge",
                previous_shop_id=previous_shop_id,
                selected_shop_id=keeper.id,
                note=note,
            )
        )

    _flatten_redirects(
        db,
        keeper=keeper,
        losing_shop_ids=losing_shop_ids,
    )
    db.flush()
    for losing_shop in losing_shops:
        db.delete(losing_shop)
    db.flush()
    return SafeDuplicateMergeResultGroup(
        shop_ids=group.shop_ids,
        keep_shop_id=group.keep_shop_id,
        evidence=group.evidence,
        action="merged",
        reason=group.reason,
        merged_shop_count=len(losing_shops),
        moved_mention_count=len(mentions),
        complemented_fields=complemented_fields,
    )


def apply_safe_duplicate_merges(db: Session) -> SafeDuplicateMergeResult:
    lock_new_shop_creation(db)
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores row locks; reserve its writer before taking any snapshots.
        db.execute(text("UPDATE shops SET id = id WHERE id = (SELECT MIN(id) FROM shops)"))
    preliminary_plan = build_safe_duplicate_merge_plan(db)
    candidate_shop_ids = tuple(
        sorted(
            {
                shop_id
                for group in preliminary_plan.groups
                for shop_id in group.shop_ids
            }
        )
    )
    if not candidate_shop_ids:
        return SafeDuplicateMergeResult(groups=())

    locked_shops = _lock_candidate_rows(db, candidate_shop_ids)
    locked_plan = _build_safe_plan_from_shops(db, locked_shops)
    shops_by_id = {shop.id: shop for shop in locked_shops}
    result_groups: list[SafeDuplicateMergeResultGroup] = []
    for group in locked_plan.groups:
        if not group.is_safe:
            result_groups.append(
                SafeDuplicateMergeResultGroup(
                    shop_ids=group.shop_ids,
                    keep_shop_id=group.keep_shop_id,
                    evidence=group.evidence,
                    action="skipped",
                    reason=group.reason,
                    merged_shop_count=0,
                    moved_mention_count=0,
                    complemented_fields=(),
                )
            )
            continue
        result_groups.append(_merge_safe_group(db, group, shops_by_id))
    db.flush()
    return SafeDuplicateMergeResult(groups=tuple(result_groups))


def build_duplicate_shadow_report(db: Session) -> DuplicateShadowReport:
    shops = (
        db.query(Shop)
        .filter(Shop.mentions.any(ShopMention.review_status == ReviewStatus.APPROVED.value))
        .order_by(Shop.id)
        .all()
    )
    grouped: dict[tuple[str, str, str], list[Shop]] = {}
    for shop in shops:
        normalized_name, normalized_branch = _normalized_shop_name(shop)
        normalized_shop_area = normalize_area(shop.area)
        if not normalized_name or not normalized_shop_area:
            continue
        grouped.setdefault(
            (normalized_name, normalized_branch, normalized_shop_area),
            [],
        ).append(shop)

    report_groups: list[DuplicateShadowGroup] = []
    for key, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        member_tuple = tuple(members)
        phones = tuple(normalize_phone(shop.phone) for shop in member_tuple)
        addresses = tuple(normalize_address(shop.address) or None for shop in member_tuple)
        evidence = DuplicateEvidence(
            phone_match=_all_present_and_equal(phones),
            address_match=_all_present_and_equal(addresses),
        )
        identity_conflicts = _conflicting_fields(member_tuple)
        user_data_conflicts = _user_data_conflicts(member_tuple)
        report_groups.append(
            DuplicateShadowGroup(
                normalized_name=key[0],
                normalized_branch=key[1],
                normalized_area=key[2],
                shop_ids=tuple(shop.id for shop in member_tuple),
                evidence=evidence,
                identity_conflicts=identity_conflicts,
                user_data_conflicts=user_data_conflicts,
                would_auto_merge=(
                    (evidence.phone_match or evidence.address_match)
                    and not identity_conflicts
                    and not user_data_conflicts
                ),
            )
        )
    return DuplicateShadowReport(groups=tuple(report_groups))

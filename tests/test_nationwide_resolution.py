import pytest

from bot.restaurant_extractor import ExtractedMention
from db.models import CandidateProvenance, Shop
from services.identification_pipeline import (
    PipelineCandidate,
    _candidate_canonical_area,
    _candidate_matches_area,
    _matches_mention_shop_identity,
    _top_web_candidate_for_new_shop,
)
from web.area_groups import area_municipality, canonicalize_area
from web.region_master import MUNICIPALITIES


def mention(area: str) -> ExtractedMention:
    return ExtractedMention(
        shop_name="食堂こもれび",
        branch_name=None,
        area=area,
        category="食堂・定食",
        needs_review=False,
        confidence_reason="投稿に店名と所在地がある",
    )


def candidate(area: str, address: str | None) -> PipelineCandidate:
    return PipelineCandidate(
        name="食堂こもれび",
        area=area,
        address=address,
        canonical_url="https://restaurant.example/komorebi",
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
    )


@pytest.mark.parametrize(
    ("area", "address"),
    [
        ("北海道上川郡美瑛町", "北海道上川郡美瑛町本町1-2-3"),
        ("長野県北安曇郡白馬村", "長野県北安曇郡白馬村北城1234"),
        ("大阪府大阪市北区", "大阪府大阪市北区梅田1-2-3"),
        ("東京都西多摩郡檜原村", "東京都西多摩郡檜原村本宿1234"),
    ],
)
def test_nationwide_municipality_requires_matching_address(
    area: str, address: str
) -> None:
    selected = _top_web_candidate_for_new_shop(mention(area), [candidate(area, address)])

    assert selected is not None
    assert selected.area is not None
    assert area_municipality(selected.area) == area_municipality(canonicalize_area(area) or "")


@pytest.mark.parametrize(
    ("area", "address"),
    [
        ("東京都府中市", "広島県府中市府川町315"),
        ("広島県府中市", "東京都府中市宮西町2-24"),
        ("北海道上川郡美瑛町", "北海道上川郡東川町東町1-2-3"),
        ("大阪府大阪市北区", "大阪府大阪市中央区本町1-2-3"),
    ],
)
def test_nationwide_municipality_rejects_conflicting_address(
    area: str, address: str
) -> None:
    assert _top_web_candidate_for_new_shop(mention(area), [candidate(area, address)]) is None


def test_municipality_alone_does_not_establish_existing_shop_identity() -> None:
    area = "東京都府中市"
    shop = Shop(shop_name="食堂こもれび", area=area)

    assert _matches_mention_shop_identity(mention(area), shop) is False
    assert _top_web_candidate_for_new_shop(mention(area), [candidate(area, None)]) is None


def test_municipality_address_needs_locality_before_street_number() -> None:
    assert _candidate_canonical_area(candidate("東京都中央区", "東京都中央区1-2-3")) is None


@pytest.mark.parametrize(
    ("area", "address"),
    [
        ("東京都府中市", "東京都府中市宮西町2-24"),
        ("千葉県印旛郡酒々井町", "千葉県印旛郡酒々井町東酒々井1-1-1"),
        ("石川県野々市市", "石川県野々市市本町1-2-3"),
        ("長崎県北松浦郡佐々町", "長崎県北松浦郡佐々町本田原免1-1"),
    ],
)
def test_municipality_address_preserves_prefecture_and_iteration_mark(
    area: str, address: str
) -> None:
    result = _candidate_canonical_area(candidate(area, address))

    assert result is not None
    assert area_municipality(result) == area_municipality(canonicalize_area(area) or "")
    assert _candidate_matches_area(result, candidate(area, address))


@pytest.mark.parametrize(
    "address",
    [
        "京都府京都市中京区本町1-2-3",
        "東京都府中市宮西町2-24京都府京都市中京区本町1-2-3",
        "東京都府中市宮西町2-24東京都府中市宮西町2-24",
        "東京都府中市宮西町2-24大阪",
        "東京都府中市宮西町2-24北広島市",
    ],
)
def test_fuchu_address_still_rejects_conflicting_or_duplicate_prefectures(
    address: str,
) -> None:
    area = canonicalize_area("東京都府中市")
    assert area is not None
    assert not _candidate_matches_area(area, candidate(area, address))
    assert _candidate_canonical_area(candidate(area, address)) is None


def test_all_municipalities_have_matching_precise_address() -> None:
    failures: list[str] = []
    for municipality in MUNICIPALITIES:
        address = f"{municipality.prefecture}{municipality.name}本町1-2-3"
        result = _candidate_canonical_area(candidate(municipality.area, address))
        if result is None or area_municipality(result) != municipality:
            failures.append(municipality.area)

    assert len(MUNICIPALITIES) == 1918
    assert failures == []


def test_tokyo_station_uses_its_municipality_and_address_leaf() -> None:
    area = canonicalize_area("東京都青梅市 / 東青梅")
    assert area is not None
    assert _candidate_matches_area(area, candidate(area, "東京都青梅市東青梅1-2-3"))
    assert not _candidate_matches_area(area, candidate(area, "東京都福生市東町1-2-3"))


@pytest.mark.parametrize(
    ("area", "address", "conflicting_address"),
    [
        (
            "東京都千代田区 / 内幸町",
            "東京都千代田区内幸町1-2-3",
            "東京都港区愛宕1-2-3",
        ),
        (
            "東京都港区 / 内幸町",
            "東京都港区愛宕1-2-3",
            "東京都千代田区内幸町1-2-3",
        ),
    ],
)
def test_station_spanning_municipalities_keeps_address_context(
    area: str, address: str, conflicting_address: str
) -> None:
    selected = _top_web_candidate_for_new_shop(
        mention(area), [candidate("内幸町駅周辺", address)]
    )
    assert selected is not None
    assert selected.area == area
    assert _top_web_candidate_for_new_shop(
        mention(area), [candidate("内幸町駅周辺", conflicting_address)]
    ) is None

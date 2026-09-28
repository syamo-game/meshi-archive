import pytest
from pydantic import ValidationError

from services.resolution import (
    CandidateIdentity,
    branches_conflict,
    evaluate_identity,
    name_similarity,
    normalize_address,
    normalize_name,
    normalize_phone,
    normalize_url_identity,
)
from bot.restaurant_extractor import ExtractedMention, extract_json_ld_candidates
from db.models import CandidateProvenance, Shop
from services.identification_pipeline import (
    BROAD_AREA_ADMIN_LOCALITIES,
    PipelineCandidate,
    _candidate_for_new_shop,
    _matches_mention_shop_identity,
    _shop_identity,
    _structured_candidate_block_reason,
    _top_web_candidate_for_new_shop,
)
from web.area_groups import AREA_TO_GROUP, CANONICAL_AREAS


def test_candidate_urls_require_http_without_userinfo() -> None:
    for invalid_url in (
        "javascript:alert(1)",
        "https://user:secret@example.com/shop",
        "//example.com/shop",
    ):
        with pytest.raises(ValidationError):
            CandidateIdentity(name="割烹みやび", canonical_url=invalid_url)


def test_url_identity_normalizes_safe_equivalent_forms() -> None:
    assert normalize_url_identity(
        "HTTPS://EXAMPLE.COM:443/shop/#section"
    ) == normalize_url_identity("https://example.com/shop")
    assert normalize_url_identity("http://example.com:80/") == "http://example.com/"


def test_url_identity_keeps_query_as_part_of_identity() -> None:
    assert normalize_url_identity("https://example.com/shop?id=1") != (
        normalize_url_identity("https://example.com/shop?id=2")
    )


def test_url_identity_errors_do_not_echo_userinfo() -> None:
    secret_url = "https://user:secret@example.com/shop"
    with pytest.raises(ValueError) as raised:
        normalize_url_identity(secret_url)
    assert "secret" not in str(raised.value)


def candidate(**values: str | None) -> CandidateIdentity:
    fields: dict[str, str | None] = {
        "name": "銀座 鮨はな 本店",
        "area": "銀座",
        "category": "寿司・回転寿司",
        "address": None,
        "phone": None,
        "canonical_url": None,
        "external_source": None,
        "external_id": None,
        "evidence_url": None,
    }
    fields.update(values)
    return CandidateIdentity.model_validate(fields)


def pipeline_candidate(
    *,
    provenance: CandidateProvenance = CandidateProvenance.WEB_SEARCH,
    is_verified: bool = False,
    verification_reason: str | None = None,
    **values: str | None,
) -> PipelineCandidate:
    identity = candidate(**values)
    return PipelineCandidate(
        **identity.model_dump(),
        provenance=provenance,
        is_verified=is_verified,
        verification_reason=verification_reason,
    )


def extracted_mention() -> ExtractedMention:
    return ExtractedMention(
        shop_name="銀座 鮨はな",
        branch_name="本店",
        area="銀座",
        category="寿司・回転寿司",
        needs_review=False,
        confidence_reason="店名、支店名、エリアあり",
    )


def test_normalization_preserves_branch_as_a_separate_check() -> None:
    assert normalize_name(" 株式会社 銀座・鮨はな 本店 ") == "銀座鮨はな本店"
    assert branches_conflict("銀座 鮨はな 本店", "銀座 鮨はな 新宿店")
    assert not branches_conflict("銀座 鮨はな 本店", "銀座 鮨はな 本店")


def test_shop_identity_includes_separate_branch_name() -> None:
    shop = Shop(
        shop_name="銀座 鮨はな",
        branch_name="本店",
        area="銀座",
        category="寿司・回転寿司",
        external_source="tabelog",
        external_id="13000001",
    )
    wrong_branch = candidate(
        name="銀座 鮨はな 新宿店",
        external_source="tabelog",
        external_id="13000001",
    )

    outcome = evaluate_identity(_shop_identity(shop), wrong_branch)

    assert outcome.is_strong_match is False
    assert "branch" in outcome.conflicting_fields


def test_external_id_exact_match_is_automatic() -> None:
    outcome = evaluate_identity(
        candidate(external_source="tabelog", external_id="13000001"),
        candidate(external_source="tabelog", external_id="13000001"),
    )
    assert outcome.is_strong_match
    assert "external_id" in outcome.matched_fields


def test_external_id_alias_and_case_normalize_to_the_same_identity() -> None:
    outcome = evaluate_identity(
        candidate(external_source="食べログ", external_id="ABC123"),
        candidate(external_source="Tabelog.com", external_id="abc123"),
    )

    assert outcome.is_strong_match
    assert "external_id" in outcome.matched_fields


def test_gurunavi_alias_normalizes_to_gnavi() -> None:
    outcome = evaluate_identity(
        candidate(external_source="Gurunavi.com", external_id="ABC123"),
        candidate(external_source="gnavi", external_id="abc123"),
    )

    assert outcome.is_strong_match
    assert "external_id" in outcome.matched_fields


def test_conflicting_id_on_the_same_service_blocks_other_strong_evidence() -> None:
    outcome = evaluate_identity(
        candidate(
            external_source="tabelog",
            external_id="13000001",
            phone="03-1234-5678",
        ),
        candidate(
            external_source="食べログ",
            external_id="13000002",
            phone="03-1234-5678",
        ),
    )

    assert outcome.is_strong_match is False
    assert "external_id" in outcome.conflicting_fields
    assert outcome.reason == "同一サービスの外部IDが矛盾"


def test_phone_requires_name_similarity_and_no_branch_conflict() -> None:
    existing = candidate(phone="03-1234-5678")
    matching = candidate(name="銀座鮨はな 本店", phone="+81 3 1234 5678")
    wrong_branch = candidate(name="銀座 鮨はな 新宿店", phone="03-1234-5678")
    assert evaluate_identity(existing, matching).is_strong_match
    assert not evaluate_identity(existing, wrong_branch).is_strong_match
    assert normalize_phone("+81 3 1234-5678") == "0312345678"


def test_phone_name_similarity_threshold_is_inclusive() -> None:
    existing = candidate(name="abcdefghij", phone="03-1234-5678")
    at_threshold = candidate(name="abcdefghxy", phone="03-1234-5678")
    below_threshold = candidate(name="abcdefgxyz", phone="03-1234-5678")

    assert name_similarity(existing.name, at_threshold.name) == pytest.approx(0.8)
    assert evaluate_identity(existing, at_threshold).is_strong_match
    assert name_similarity(existing.name, below_threshold.name) < 0.8
    assert not evaluate_identity(existing, below_threshold).is_strong_match


def test_address_requires_high_name_similarity() -> None:
    address = "東京都中央区銀座1丁目2番3号"
    normalized = normalize_address(address)
    assert normalized
    assert evaluate_identity(candidate(address=address), candidate(address=address)).is_strong_match
    assert not evaluate_identity(
        candidate(address=address),
        candidate(name="別のレストラン", address=address),
    ).is_strong_match


def test_address_name_similarity_threshold_is_inclusive() -> None:
    address = "東京都中央区銀座1丁目2番3号"
    existing = candidate(name="abcdefghij", address=address)
    at_threshold = candidate(name="abcdefghix", address=address)
    below_threshold = candidate(name="abcdefghxy", address=address)

    assert name_similarity(existing.name, at_threshold.name) == pytest.approx(0.9)
    assert evaluate_identity(existing, at_threshold).is_strong_match
    assert name_similarity(existing.name, below_threshold.name) < 0.9
    assert not evaluate_identity(existing, below_threshold).is_strong_match


def test_name_and_area_alone_are_never_automatic() -> None:
    outcome = evaluate_identity(candidate(), candidate(name="銀座鮨はな 本店"))
    assert not outcome.is_strong_match


def test_unverified_web_candidate_cannot_create_shop() -> None:
    search_result = pipeline_candidate(
        canonical_url="https://sushihana.example/ginza-honten",
        evidence_url="https://directory.example/ginza-honten",
    )
    assert _candidate_for_new_shop(extracted_mention(), [search_result]) is None


def test_single_verified_structured_candidate_can_create_shop() -> None:
    structured_result = pipeline_candidate(
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
        canonical_url="https://sushihana.example/ginza-honten",
        evidence_url="https://sushihana.example/ginza-honten",
    )
    assert (
        _candidate_for_new_shop(extracted_mention(), [structured_result])
        == structured_result
    )


def test_verified_structured_candidate_fills_missing_area_from_address() -> None:
    mention = extracted_mention().model_copy(update={"area": None})
    structured_result = pipeline_candidate(
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        area="東京都中央区",
        address="東京都中央区銀座1-2-3",
        canonical_url="https://sushihana.example/ginza-honten",
        evidence_url="https://sushihana.example/ginza-honten",
    )

    selected = _candidate_for_new_shop(mention, [structured_result])

    assert selected is not None
    assert selected.area == "銀座"


def test_verified_structured_candidate_must_be_unique() -> None:
    candidates = [
        pipeline_candidate(
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            canonical_url="https://official.example/ginza-honten",
            evidence_url="https://official.example/ginza-honten",
        ),
        pipeline_candidate(
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            canonical_url="https://directory.example/ginza-honten",
            evidence_url="https://directory.example/ginza-honten",
        ),
    ]
    assert _candidate_for_new_shop(extracted_mention(), candidates) is None


def test_structured_candidates_from_one_page_must_not_hide_another_shop() -> None:
    page_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"
    identities = extract_json_ld_candidates(
        """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@graph": [
            {
              "@type": "Restaurant",
              "name": "Cafe Alpha",
              "address": "東京都中央区銀座1-2-3"
            },
            {
              "@type": "Restaurant",
              "name": "Cafe Beta",
              "address": "東京都新宿区新宿1-2-3"
            }
          ]
        }
        </script>
        """,
        page_url,
    )
    candidates = [
        PipelineCandidate(
            **identity.model_dump(),
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )
        for identity in identities
    ]
    mention = ExtractedMention(
        shop_name="Cafe Alpha",
        branch_name=None,
        area="銀座",
        category="カフェ・喫茶店",
        needs_review=False,
        confidence_reason="店名とエリアあり",
    )

    assert len(candidates) == 2
    assert _candidate_for_new_shop(mention, candidates) is None


def test_verified_structured_candidate_rejects_branch_and_area_conflicts() -> None:
    wrong_branch = pipeline_candidate(
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        name="銀座 鮨はな 新宿店",
        canonical_url="https://official.example/shinjuku",
    )
    wrong_area = pipeline_candidate(
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        area="新宿",
        canonical_url="https://official.example/ginza-honten",
    )
    assert _candidate_for_new_shop(extracted_mention(), [wrong_branch]) is None
    assert _candidate_for_new_shop(extracted_mention(), [wrong_area]) is None


def test_posted_external_id_cannot_create_shop_without_page_identity() -> None:
    assert _candidate_for_new_shop(extracted_mention(), []) is None


def test_two_unverified_web_sources_cannot_create_shop() -> None:
    candidates = [
        pipeline_candidate(
            phone="03-1234-5678",
            evidence_url="https://official.example/shop",
        ),
        pipeline_candidate(
            phone="0312345678",
            evidence_url="https://directory.example/shop",
        ),
    ]
    assert _candidate_for_new_shop(extracted_mention(), candidates) is None


def test_top_web_matching_score_threshold_is_inclusive() -> None:
    mention = ExtractedMention(
        shop_name="abc",
        branch_name="本店",
        area="銀座",
        category="寿司・回転寿司",
        needs_review=False,
        confidence_reason="店名とエリアあり",
    )
    at_threshold = pipeline_candidate(
        name="abx 本店",
        area="銀座",
        canonical_url="https://directory.example/at-threshold",
    )
    below_threshold = pipeline_candidate(
        name="axy 本店",
        area="銀座",
        canonical_url="https://directory.example/below-threshold",
    )

    selected = _top_web_candidate_for_new_shop(mention, [at_threshold])

    assert name_similarity("abc 本店", at_threshold.name) == pytest.approx(0.8)
    assert selected is not None
    assert "policy=unique_top_web_matching_score_080" in (
        selected.verification_reason or ""
    )
    assert "matching_score=0.800" in (selected.verification_reason or "")
    assert "source_verified=false" in (selected.verification_reason or "")
    assert _top_web_candidate_for_new_shop(mention, [below_threshold]) is None


def test_unique_top_web_matching_score_selects_only_highest_candidate() -> None:
    highest = pipeline_candidate(
        canonical_url="https://directory.example/ginza-honten",
    )
    lower = pipeline_candidate(
        name="銀座 鮨はな",
        canonical_url="https://directory.example/ginza",
    )

    assert (
        _top_web_candidate_for_new_shop(extracted_mention(), [lower, highest])
        == highest.model_copy(
            update={
                "verification_reason": (
                    "policy=unique_top_web_matching_score_080; "
                    "matching_score=1.000; canonical_area=銀座; "
                    "source_verified=false"
                )
            }
        )
    )


def test_web_candidate_cannot_add_area_as_an_unmentioned_branch() -> None:
    mention = ExtractedMention(
        shop_name="焼肉ホルモンたけ田",
        branch_name=None,
        area="新宿",
        category="焼肉",
        needs_review=False,
        confidence_reason="店名とエリアあり",
    )
    search_result = pipeline_candidate(
        name="焼肉ホルモンたけ田 新宿",
        area="新宿",
        address="東京都新宿区新宿1-1-1",
        canonical_url="https://directory.example/takeda-shinjuku",
        evidence_url="https://directory.example/takeda-shinjuku",
    )

    assert name_similarity(mention.shop_name, search_result.name) >= 0.8
    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


def test_tied_top_web_matching_scores_are_not_selected() -> None:
    candidates = [
        pipeline_candidate(
            canonical_url="https://directory-one.example/ginza-honten",
        ),
        pipeline_candidate(
            canonical_url="https://directory-two.example/ginza-honten",
        ),
    ]

    assert _top_web_candidate_for_new_shop(extracted_mention(), candidates) is None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("未知の街", "未知の街", "東京都未知区未知の街1-2-3"),
        ("銀座", "新宿", "東京都新宿区新宿1-2-3"),
    ],
)
def test_top_web_matching_score_requires_matching_canonical_area(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/shop",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


def test_top_web_matching_score_accepts_canonical_leaf_in_address() -> None:
    search_result = pipeline_candidate(
        area=None,
        address="東京都中央区銀座1-2-3",
        canonical_url="https://directory.example/ginza-honten",
    )

    assert (
        _top_web_candidate_for_new_shop(extracted_mention(), [search_result])
        is not None
    )


def test_top_web_matching_score_uses_address_when_candidate_area_is_not_canonical() -> None:
    search_result = pipeline_candidate(
        area="東京都中央区",
        address="東京都中央区銀座1-2-3",
        canonical_url="https://directory.example/ginza-honten",
    )

    assert (
        _top_web_candidate_for_new_shop(extracted_mention(), [search_result])
        is not None
    )


def test_top_web_matching_score_fills_missing_area_from_precise_address() -> None:
    mention = extracted_mention().model_copy(update={"area": None})
    search_result = pipeline_candidate(
        area="東京都中央区",
        address="東京都中央区銀座1-2-3",
        canonical_url="https://directory.example/ginza-honten",
    )

    selected = _top_web_candidate_for_new_shop(mention, [search_result])

    assert selected is not None
    assert selected.area == "銀座"


def test_top_web_matching_score_does_not_fill_missing_area_without_address() -> None:
    mention = extracted_mention().model_copy(update={"area": None})
    search_result = pipeline_candidate(
        area="銀座",
        address=None,
        canonical_url="https://directory.example/ginza-honten",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


def test_top_web_matching_score_rejects_ambiguous_address_area() -> None:
    mention = extracted_mention().model_copy(update={"area": None})
    search_result = pipeline_candidate(
        area="東京都中央区",
        address="東京都中央区1-2-3",
        canonical_url="https://directory.example/chuo",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


def test_top_web_matching_score_rejects_conflicting_raw_mention_area() -> None:
    mention = extracted_mention().model_copy(update={"area": "東京都新宿区"})
    search_result = pipeline_candidate(
        area="東京都中央区",
        address="東京都中央区銀座1-2-3",
        canonical_url="https://directory.example/ginza-honten",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


def test_top_web_matching_score_rejects_explicit_area_conflict_even_if_address_matches() -> None:
    search_result = pipeline_candidate(
        area="新宿",
        address="東京都中央区銀座1-2-3 新宿支店受付",
        canonical_url="https://directory.example/shinjuku",
    )

    assert _top_web_candidate_for_new_shop(extracted_mention(), [search_result]) is None


@pytest.mark.parametrize(
    ("mention_area", "candidate_address"),
    [
        ("銀座", "東京都新宿区新宿1-2-3"),
        ("吉祥寺", "東京都三鷹市下連1-2-3"),
        ("銀座", "東京都八王子市旭町1-2-3"),
        ("川崎", "神奈川県相模原市中央区1-2-3"),
        ("札幌", "北海道函館市本町1-2-3"),
        ("名古屋", "愛知県豊橋市駅前大通1-2-3"),
        ("名古屋", "愛知県豊橋市名古屋ビル1-2-3"),
        ("名古屋", "愛知県名古屋市中区1-2-3，豊橋市駅前大通1-2-3"),
        ("名古屋", "愛知県名古屋市中区1-2-3,豊橋市駅前大通1-2-3"),
        ("名古屋", "愛知県名古屋市中区1-2-3・豊橋市駅前大通1-2-3"),
        ("名古屋", "愛知県名古屋市中区1-2-3\n豊橋市駅前大通1-2-3"),
        ("名古屋", "愛知県名古屋市中区および豊橋市駅前大通"),
        ("名古屋", "愛知県名古屋市中区及び豊橋市駅前大通"),
        ("名古屋", "愛知県名古屋市中区（豊橋市駅前大通）"),
        ("名古屋", "愛知県名古屋市中区(豊橋市駅前大通)"),
        ("名古屋", "愛知県名古屋市中区 豊橋市駅前大通"),
        ("大間", "青森県下北郡大間町（風間浦村）"),
        ("大間", "青森県下北郡大間町 七飯町"),
        ("大間", "青森県下北郡大間町（むつ市）"),
        ("大間", "青森県下北郡大間町（中央区）"),
        ("五島列島", "長崎県南松浦郡新上五島町（北松浦郡小値賀町）"),
        ("金沢", "石川県小松市園町1-2-3"),
        ("札幌", "北海道函館市札幌ビル1-2-3"),
    ],
)
def test_top_web_matching_score_rejects_address_conflicting_with_canonical_area(
    mention_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=mention_area,
        address=candidate_address,
        canonical_url="https://directory.example/conflicting-address",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


def test_top_web_matching_score_rejects_noncanonical_parent_area_conflict() -> None:
    search_result = pipeline_candidate(
        area="東京都新宿区",
        address="東京都中央区銀座1-2-3",
        canonical_url="https://directory.example/conflicting-parent",
    )

    assert _top_web_candidate_for_new_shop(extracted_mention(), [search_result]) is None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("品川", "東京都品川区", "東京都品川区大井1-2-3"),
        ("白山", "石川県白山市", "石川県白山市倉光1-2-3"),
        ("銀座", "東京都中央区・新宿", "東京都中央区銀座1-2-3"),
        ("銀座", "東京都中央区および新宿区", "東京都中央区銀座1-2-3"),
        ("銀座", "東京都中央区・京都", "東京都中央区銀座1-2-3"),
        ("金沢", "大阪府大阪市金沢店", "大阪府大阪市金沢ビル1-2-3"),
        ("川崎", "神奈川県横浜市", "神奈川県横浜市川崎ビル1-2-3"),
        (
            "名古屋",
            "愛知県名古屋市・豊橋市",
            "愛知県名古屋市中区1-2-3",
        ),
        (
            "名古屋",
            "愛知県名古屋市および豊橋市",
            "愛知県名古屋市中区1-2-3",
        ),
        (
            "札幌",
            "札幌市中央区および函館市",
            "北海道札幌市中央区北1条西1-2-3",
        ),
        (
            "名古屋",
            "愛知県名古屋市",
            "愛知県名古屋市中区1-2-3 / 愛知県豊橋市駅前大通1-2-3",
        ),
        (
            "銀座",
            "東京都中央区",
            "東京都中央区銀座1-2-3 / 東京都新宿区新宿1-2-3",
        ),
    ],
)
def test_top_web_matching_score_rejects_ambiguous_noncanonical_area(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/ambiguous-area",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


@pytest.mark.parametrize("area", ["市場前駅", "東陽町駅"])
def test_top_web_matching_score_accepts_noncanonical_station_suffix(area: str) -> None:
    canonical_area = area.removesuffix("駅")
    mention = extracted_mention().model_copy(update={"area": canonical_area})
    search_result = pipeline_candidate(
        area=area,
        address=f"東京都江東区{canonical_area}1-2-3",
        canonical_url="https://directory.example/station-suffix",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


def test_top_web_matching_score_accepts_matching_prefecture_and_city_parent() -> None:
    mention = extracted_mention().model_copy(update={"area": "川崎"})
    search_result = pipeline_candidate(
        area="神奈川県川崎市",
        address="神奈川県川崎市川崎区駅前本町1-2-3",
        canonical_url="https://directory.example/kawasaki",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("名古屋", "愛知県名古屋市 中区", "愛知県名古屋市 中区栄1-2-3"),
        ("札幌", "北海道札幌市 中央区", "北海道札幌市 中央区北1条西1-2-3"),
        ("川崎", "神奈川県川崎市 川崎区", "神奈川県川崎市 川崎区駅前本町1-2-3"),
        ("塩釜", "宮城県塩竈市", "宮城県塩竈市尾島町1-2-3"),
        ("名古屋", "愛知県名古屋市中区", "愛知県名古屋市中区（名古屋市民会館）"),
        ("札幌", "北海道札幌市中央区", "北海道札幌市中央区 札幌市民ホール"),
        ("銀座", "東京都中央区", "東京都中央区（区立会館）銀座1-2-3"),
        ("大間", "青森県下北郡大間町", "青森県下北郡大間町 本町1-2-3"),
    ],
)
def test_top_web_matching_score_accepts_valid_admin_spacing_and_aliases(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/valid-admin-address",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("銀座", "八王子市", "東京都中央区銀座1-2-3"),
        ("川崎", "相模原市", "神奈川県川崎市川崎区駅前本町1-2-3"),
        ("名古屋", "豊橋市", "愛知県名古屋市中区1-2-3"),
        ("札幌", "函館市", "北海道札幌市中央区北1条西1-2-3"),
        ("白山", "白山市", "東京都文京区白山1-2-3"),
        ("名古屋", "愛知県東郷町", "愛知県名古屋市中区1-2-3"),
        ("札幌", "北海道七飯町", "北海道札幌市中央区北1条西1-2-3"),
    ],
)
def test_top_web_matching_score_rejects_candidate_municipality_conflict(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/conflicting-municipality",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        (
            "札幌",
            "函館市",
            "北海道札幌市中央区函館市アンテナショップ1-2-3",
        ),
        (
            "名古屋",
            "豊橋市",
            "愛知県名古屋市中区豊橋市民会館1-2-3",
        ),
        (
            "大間",
            "七飯町",
            "青森県下北郡大間町七飯町物産館1-2-3",
        ),
    ],
)
def test_top_web_matching_score_rejects_candidate_municipality_in_building_name(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/municipality-building-name",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        (
            "札幌",
            "北海道札幌市北区",
            "北海道札幌市中央区北区民センター1-2-3",
        ),
        (
            "名古屋",
            "愛知県名古屋市西区",
            "愛知県名古屋市中区西区民会館1-2-3",
        ),
        (
            "川崎",
            "神奈川県川崎市幸区",
            "神奈川県川崎市川崎区幸区民館1-2-3",
        ),
    ],
)
def test_top_web_matching_score_rejects_candidate_ward_in_building_name(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/ward-building-name",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        (
            "名古屋",
            "愛知県名古屋市 名駅",
            "愛知県名古屋市中村区名駅1-2-3",
        ),
        (
            "札幌",
            "北海道札幌市 大通",
            "北海道札幌市中央区大通西1-2-3",
        ),
    ],
)
def test_top_web_matching_score_accepts_ward_between_city_and_locality(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/city-ward-locality",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


def test_broad_area_admin_localities_cover_remaining_flat_areas() -> None:
    flat_areas = {
        area for area in CANONICAL_AREAS if " / " not in AREA_TO_GROUP[area]
    }

    assert flat_areas <= set(BROAD_AREA_ADMIN_LOCALITIES)
    assert set(BROAD_AREA_ADMIN_LOCALITIES) <= CANONICAL_AREAS


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("大間", "青森県下北郡大間町", "青森県下北郡大間町大間1-2-3"),
        (
            "五島列島",
            "長崎県南松浦郡新上五島町",
            "長崎県南松浦郡新上五島町青方郷1-2-3",
        ),
        ("五島列島", "長崎県五島市", "長崎県五島市福江町1-2-3"),
    ],
)
def test_top_web_matching_score_accepts_broad_area_admin_localities(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/broad-area-locality",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("富山", "富山市", "富山県富山市桜町1-2-3"),
        ("岐阜", "岐阜市", "岐阜県岐阜市神田町1-2-3"),
        ("徳島", "徳島市", "徳島県徳島市寺島本町1-2-3"),
        ("高知", "高知市", "高知県高知市帯屋町1-2-3"),
        ("清水五条", "京都市", "京都府京都市東山区五条橋東1-2-3"),
    ],
)
def test_top_web_matching_score_keeps_region_named_admin_locality(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/region-named-locality",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


def test_top_web_matching_score_ignores_ward_name_from_another_region_hierarchy() -> None:
    mention = extracted_mention().model_copy(update={"area": "札幌"})
    search_result = pipeline_candidate(
        area="北海道札幌市中央区",
        address="北海道札幌市中央区北1条西1-2-3",
        canonical_url="https://directory.example/sapporo",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


@pytest.mark.parametrize(
    ("mention_area", "candidate_area", "candidate_address"),
    [
        ("札幌", "中央区", "北海道札幌市中央区北1条西1-2-3"),
        ("品川", "品川駅", "東京都港区港南2-16-3"),
    ],
)
def test_top_web_matching_score_accepts_leaf_or_address_at_different_granularity(
    mention_area: str,
    candidate_area: str,
    candidate_address: str,
) -> None:
    mention = extracted_mention().model_copy(update={"area": mention_area})
    search_result = pipeline_candidate(
        area=candidate_area,
        address=candidate_address,
        canonical_url="https://directory.example/different-granularity",
    )

    assert _top_web_candidate_for_new_shop(mention, [search_result]) is not None


def test_top_web_matching_score_rejects_branch_conflict() -> None:
    wrong_branch = pipeline_candidate(
        name="銀座 鮨はな 新宿店",
        canonical_url="https://directory.example/shinjuku",
    )

    assert _top_web_candidate_for_new_shop(extracted_mention(), [wrong_branch]) is None


def test_top_web_matching_score_requires_extracted_branch_in_candidate_name() -> None:
    branch_omitted = pipeline_candidate(
        name="銀座 鮨はな",
        canonical_url="https://directory.example/ginza",
    )

    assert _top_web_candidate_for_new_shop(extracted_mention(), [branch_omitted]) is None


def test_normal_web_candidate_requires_exact_name_without_mention_branch() -> None:
    mention = extracted_mention().model_copy(
        update={
            "shop_name": "スターバックス",
            "branch_name": None,
            "area": "新宿",
        }
    )
    ambiguous_branch = pipeline_candidate(
        name="スターバックス 西新宿",
        area="新宿",
        evidence_url="https://directory.example/starbucks-nishi-shinjuku",
    )
    exact_name = ambiguous_branch.model_copy(update={"name": "スターバックス"})

    assert name_similarity(mention.shop_name, ambiguous_branch.name) >= 0.8
    assert _top_web_candidate_for_new_shop(mention, [ambiguous_branch]) is None
    assert _top_web_candidate_for_new_shop(mention, [exact_name]) is not None

    variant_mention = mention.model_copy(update={"shop_name": "焼肉ホルモンたけ田"})
    unseparated_branch = ambiguous_branch.model_copy(
        update={"name": "焼肉ホルモンたけだ東口"}
    )
    assert name_similarity(variant_mention.shop_name, unseparated_branch.name) >= 0.8
    assert _top_web_candidate_for_new_shop(
        variant_mention,
        [unseparated_branch],
    ) is None


def test_municipality_alone_cannot_match_existing_shop_identity() -> None:
    mention = extracted_mention().model_copy(update={"area": "台東区"})
    shop = Shop(
        shop_name="銀座 鮨はな",
        branch_name="本店",
        area="台東区",
    )

    assert _matches_mention_shop_identity(mention, shop) is False


def test_top_web_matching_score_requires_evidence_url() -> None:
    search_result = pipeline_candidate(
        canonical_url=None,
        evidence_url=None,
    )

    assert _top_web_candidate_for_new_shop(extracted_mention(), [search_result]) is None


def test_verified_structured_candidate_precedes_web_matching_score_policy() -> None:
    structured = pipeline_candidate(
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
        canonical_url="https://official.example/ginza-honten",
    )
    web = pipeline_candidate(
        canonical_url="https://directory.example/ginza-honten",
    )

    assert _candidate_for_new_shop(extracted_mention(), [web, structured]) == structured


def test_web_fallback_does_not_bypass_multiple_structured_candidates() -> None:
    mention = extracted_mention().model_copy(
        update={
            "shop_name": "Cafe Alpha",
            "branch_name": None,
            "area": "銀座",
        }
    )
    first = pipeline_candidate(
        name="Cafe Alpha",
        area="銀座",
        address="東京都中央区銀座1-1-1",
        canonical_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
        external_source="tabelog",
        external_id="13000001",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )
    second = first.model_copy(
        update={
            "address": "東京都中央区銀座9-9-9",
            "canonical_url": "https://tabelog.com/tokyo/A1301/A130101/13000002/",
            "external_id": "13000002",
        }
    )
    web = pipeline_candidate(
        name="Cafe Alpha",
        area="銀座",
        address=first.address,
        evidence_url=first.canonical_url,
    )
    candidates = [first, second, web]

    assert _candidate_for_new_shop(mention, candidates) is None
    assert _structured_candidate_block_reason(mention, candidates) is not None
    assert _top_web_candidate_for_new_shop(mention, candidates) is None

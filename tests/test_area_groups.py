import pytest

from bot.restaurant_extractor import ExtractedMention
from web.area_groups import (
    AREA_TO_GROUP,
    AreaFilterOption,
    area_filter_label,
    area_filter_matches,
    area_from_address,
    area_is_municipality,
    area_municipality,
    build_area_filter_options,
    canonicalize_area,
    editable_area_groups,
    is_known_area,
    municipalities_in_text,
)
from web.region_master import MUNICIPALITIES, TOKYO_STATIONS


EXPECTED_NEW_AREAS = {
    "りんくう常滑": "愛知 / 常滑市",
    "中野": "東京 / 中野区",
    "勝どき": "東京 / 中央区",
    "品川": "東京 / 港区",
    "四谷三丁目": "東京 / 新宿区",
    "多摩センター": "東京 / 多摩市",
    "大門": "東京 / 港区",
    "川崎": "神奈川 / 川崎市",
    "新大橋": "東京 / 江東区",
    "月島": "東京 / 中央区",
    "東中野": "東京 / 中野区",
    "東大島": "東京 / 江東区",
    "東陽町": "東京 / 江東区",
    "田無": "東京 / 西東京市",
    "田町": "東京 / 港区",
    "砂町銀座": "東京 / 江東区",
    "経堂": "東京 / 世田谷区",
    "西葛西": "東京 / 江戸川区",
    "西麻布": "東京 / 港区",
    "赤坂見附": "東京 / 港区",
    "逗子": "神奈川 / 逗子市",
    "霞が関": "東京 / 千代田区",
}

EXPECTED_AREA_ALIASES = {
    "中目黒駅": "中目黒",
    "森下三丁目": "森下",
    "学芸大学駅": "学芸大学",
    "門前仲町駅": "門前仲町",
    "埼玉県草加市": "埼玉県草加市",
    "初音小路": "谷中",
    "初音小路（谷中）": "谷中",
    "コレド室町": "日本橋",
    "コレド室町1": "日本橋",
    "東京虎ノ門": "虎ノ門",
    "東京都大田区池上": "池上",
    "山王健保会館": "赤坂",
    "上野御徒町": "御徒町",
    "菊川三丁目": "菊川",
    "菊川（東京都墨田区）": "菊川",
    "陸前高田市": "陸前高田",
}

EXPECTED_CANONICAL_AREA_GROUPS = {
    "原宿": "東京 / 渋谷区",
    "六本木": "東京 / 港区",
    "新宿三丁目": "東京 / 新宿区",
    "浦和": "埼玉 / さいたま市",
    "大宮": "埼玉 / さいたま市",
    "知立": "愛知 / 知立市",
    "祇園四条": "京都 / 京都市東山区",
    "湯島": "東京 / 文京区",
}


def test_all_legacy_unclassified_areas_have_groups_without_renaming() -> None:
    assert {area: AREA_TO_GROUP[area] for area in EXPECTED_NEW_AREAS} == EXPECTED_NEW_AREAS


@pytest.mark.parametrize(
    ("value", "expected"),
    EXPECTED_AREA_ALIASES.items(),
)
def test_area_aliases_are_canonicalized(value: str, expected: str) -> None:
    assert canonicalize_area(value) == expected
    assert is_known_area(value) is True


def test_area_canonicalization_normalizes_width_and_outer_space() -> None:
    assert canonicalize_area("　中目黒駅　") == "中目黒"


def test_new_canonical_areas_are_grouped() -> None:
    assert {
        area: AREA_TO_GROUP[area]
        for area in EXPECTED_CANONICAL_AREA_GROUPS
    } == EXPECTED_CANONICAL_AREA_GROUPS
    assert all(is_known_area(area) for area in EXPECTED_CANONICAL_AREA_GROUPS)


@pytest.mark.parametrize(
    "value",
    [
        "東京",
        "京都",
        "東京国際フォーラム",
        "門前仲町/木場",
        "　架空エリア　",
    ],
)
def test_unknown_or_wrong_granularity_area_is_not_known(value: str) -> None:
    assert canonicalize_area(value) is None
    assert is_known_area(value) is False


@pytest.mark.parametrize("value", [None, "", "　"])
def test_empty_area_has_no_canonical_value(value: str | None) -> None:
    assert canonicalize_area(value) is None
    assert is_known_area(value) is False


@pytest.mark.parametrize("value", ["札幌", "仙台", "名古屋", "那覇空港"])
def test_regional_city_and_transport_hub_labels_remain_managed(value: str) -> None:
    assert canonicalize_area(value) == value
    assert is_known_area(value) is True


def test_kappo_is_a_valid_category() -> None:
    mention = ExtractedMention(
        shop_name="割烹みやび",
        branch_name=None,
        area="銀座",
        category="割烹",
        needs_review=False,
        confidence_reason="投稿本文に店名とカテゴリがある",
    )
    assert mention.category == "割烹"


def test_official_master_covers_every_prefecture_and_tokyo_municipality() -> None:
    assert {row.prefecture_code for row in MUNICIPALITIES} == {f"{index:02}" for index in range(1, 48)}
    tokyo = [row for row in MUNICIPALITIES if row.prefecture_code == "13"]
    assert len(tokyo) == 62
    assert sum(row.municipality.endswith("区") for row in tokyo) == 23
    assert sum(row.municipality.endswith("市") for row in tokyo) == 26
    assert sum(row.municipality.endswith("町") for row in tokyo) == 5
    assert sum(row.municipality.endswith("村") for row in tokyo) == 8
    assert len({row.code for row in MUNICIPALITIES}) == len(MUNICIPALITIES)
    assert all(row.area in AREA_TO_GROUP for row in MUNICIPALITIES)
    assert all(canonicalize_area(row.area) == row.area for row in MUNICIPALITIES)
    assert all(area_is_municipality(row.area) for row in MUNICIPALITIES)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("台東区", "東京都台東区"),
        ("東京都檜原村", "東京都西多摩郡檜原村"),
        ("長野県小布施町", "長野県上高井郡小布施町"),
        ("沖縄県与那国町", "沖縄県八重山郡与那国町"),
        ("大阪府大阪市北区", "大阪府大阪市北区"),
        ("神奈川県横浜市中区", "神奈川県横浜市中区"),
    ],
)
def test_municipality_names_are_canonicalized(value: str, expected: str) -> None:
    assert canonicalize_area(value) == expected
    assert area_is_municipality(value)
    assert area_municipality(value) is not None


@pytest.mark.parametrize("value", ["中央区", "北区", "朝日町", "府中市"])
def test_ambiguous_unqualified_municipality_is_not_guessed(value: str) -> None:
    assert canonicalize_area(value) is None


def test_same_name_municipalities_have_different_codes() -> None:
    tokyo = area_municipality("東京都府中市")
    hiroshima = area_municipality("広島県府中市")
    assert tokyo is not None and hiroshima is not None
    assert tokyo.code != hiroshima.code
    assert tokyo.prefecture == "東京都"
    assert hiroshima.prefecture == "広島県"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("東京都中野区 / 東中野", "東中野"),
        ("東京都多摩市 / 多摩センター", "多摩センター"),
        ("東京都千代田区丸の内", "丸の内"),
        ("東京都中央区 / 銀座駅", "銀座"),
    ],
)
def test_qualified_existing_neighborhoods_keep_legacy_values(value: str, expected: str) -> None:
    assert canonicalize_area(value) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("東京都中央区銀座1-2-3", "銀座"),
        ("東京都文京区湯島3-1-1", "湯島"),
        ("東京都 / 千代田区丸の内1-1", "丸の内"),
        ("東京都：港区赤坂1-1", "赤坂"),
        ("東京都・港区赤坂1-1", "赤坂"),
        ("東京都台東区1-2-3", "東京都台東区"),
        ("東京都西多摩郡檜原村本宿1", "東京都西多摩郡檜原村"),
        ("長野県上高井郡小布施町大字小布施1", "長野県上高井郡小布施町"),
        ("大阪府大阪市北区梅田1-2-3", "大阪府大阪市北区"),
        ("神奈川県横浜市中区山下町1", "神奈川県横浜市中区"),
        ("広島県府中市府中町1", "広島県府中市"),
        ("銀座1-2-3", None),
        ("東京都府中市/広島県府中市", None),
        ("東京都新宿区/渋谷区", None),
    ],
)
def test_address_classification_uses_municipality_context(address: str, expected: str | None) -> None:
    assert area_from_address(address) == expected


def test_city_ward_address_selects_ward_without_parent_city_duplicate() -> None:
    rows = municipalities_in_text("大阪府大阪市北区梅田1-2-3")
    assert len(rows) == 1
    assert rows[0].name == "大阪市北区"


def test_station_coverage_and_municipal_parent_are_real_master_entries() -> None:
    assert len({row.station_name for row in TOKYO_STATIONS}) > 500
    for name in ("立川", "高尾", "奥多摩", "西武新宿"):
        candidates = [row for row in TOKYO_STATIONS if row.station_name == name]
        assert candidates, name
        for row in candidates:
            parent = next(item for item in MUNICIPALITIES if item.code == row.municipality_code)
            area = canonicalize_area(f"{parent.area}{name}駅")
            assert area is not None
            assert area_municipality(area) == parent


def test_edit_choices_include_municipalities_without_station_or_shops() -> None:
    groups = editable_area_groups()
    options = {area for _, items in groups for area, _ in items}
    assert {row.area for row in MUNICIPALITIES} <= options
    assert "東京都御蔵島村" in options
    assert "東京都小笠原村" in options
    assert "東京都西多摩郡檜原村" in options
    assert "銀座" in options


def test_area_filter_headings_include_direct_shops_and_each_child_once() -> None:
    options = build_area_filter_options([
        ("東京都江東区 / 木場", 1),
        ("門前仲町", 3),
        ("東京都江東区", 2),
        ("東京都江東区 / 新木場", 4),
        ("江東区", 5),
    ])
    assert options == [
        AreaFilterOption("東京都江東区", "東京 / 江東区", 15, 0, True),
        AreaFilterOption("江東区", "江東区", 5, 1, False),
        AreaFilterOption("東京都江東区 / 新木場", "新木場", 4, 1, False),
        AreaFilterOption("門前仲町", "門前仲町", 3, 1, False),
        AreaFilterOption("東京都江東区 / 木場", "木場", 1, 1, False),
    ]


def test_saved_municipality_alias_remains_selectable_by_its_existing_exact_value() -> None:
    options = build_area_filter_options([("江東区", 3), ("東京都江東区", 2)])
    alias = next(option for option in options if option.value == "江東区")
    heading = next(option for option in options if option.value == "東京都江東区")
    assert alias == AreaFilterOption("江東区", "江東区", 3, 1, False)
    assert heading.count == 5
    assert area_filter_matches("江東区", alias.value)
    assert not area_filter_matches("東京都江東区", alias.value)


@pytest.mark.parametrize(
    ("stored", "selected", "expected"),
    [
        ("東京都江東区", "東京都江東区", True),
        ("江東区", "東京都江東区", True),
        ("門前仲町", "東京都江東区", True),
        ("東京都江東区 / 木場", "東京都江東区", True),
        ("東京都江東区 / 新木場", "東京都江東区 / 木場", False),
        ("木場", "東京都江東区 / 木場", False),
        ("木場", "東京都江東区", True),
        ("門前仲町駅", "門前仲町", False),
        ("銀座", "東京都江東区", False),
        ("東京都江東区", "江東区", False),
        ("東京", "東京都千代田区", True),
        ("陸前高田市", "岩手県陸前高田市", True),
        ("陸前高田", "陸前高田市", False),
        ("野毛", "神奈川県横浜市", True),
        ("神奈川県横浜市中区", "神奈川県横浜市", True),
        ("野毛", "神奈川県横浜市中区", False),
        ("神奈川県横浜市", "神奈川県横浜市中区", False),
        ("神奈川県横浜市西区", "神奈川県横浜市中区", False),
        ("神奈川県川崎市中原区", "神奈川県横浜市", False),
        ("大宮", "埼玉県さいたま市", True),
        ("大宮", "埼玉県さいたま市大宮区", False),
        ("東京都府中市", "広島県府中市", False),
        ("広島県府中市", "東京都府中市", False),
        ("府中市", "東京都府中市", False),
    ],
)
def test_area_filter_matches_headings_without_broadening_leaf_selections(
    stored: str, selected: str, expected: bool,
) -> None:
    assert area_filter_matches(stored, selected) is expected


def test_city_filter_contains_ward_headings_and_keeps_coarse_legacy_areas_at_city_level() -> None:
    options = build_area_filter_options([
        ("神奈川県横浜市西区", 1),
        ("神奈川県横浜市", 2),
        ("野毛", 3),
        ("神奈川県横浜市中区", 4),
    ])
    assert options == [
        AreaFilterOption("神奈川県横浜市", "神奈川 / 横浜市", 10, 0, True),
        AreaFilterOption("野毛", "野毛", 3, 1, False),
        AreaFilterOption("神奈川県横浜市西区", "神奈川 / 横浜市西区", 1, 1, True),
        AreaFilterOption("神奈川県横浜市中区", "神奈川 / 横浜市中区", 4, 1, True),
    ]


def test_city_heading_is_available_when_only_ward_neighborhoods_have_shops() -> None:
    assert build_area_filter_options([("祇園四条", 2), ("清水五条", 1)]) == [
        AreaFilterOption("京都府京都市", "京都 / 京都市", 3, 0, True),
        AreaFilterOption("京都府京都市東山区", "京都 / 京都市東山区", 3, 1, True),
        AreaFilterOption("祇園四条", "祇園四条", 2, 2, False),
        AreaFilterOption("清水五条", "清水五条", 1, 2, False),
    ]


def test_area_filter_legacy_aliases_keep_existing_headings_and_exact_child_values() -> None:
    assert build_area_filter_options([("東京", 2), ("陸前高田市", 3), ("陸前高田", 1)]) == [
        AreaFilterOption("岩手県陸前高田市", "岩手 / 陸前高田市", 4, 0, True),
        AreaFilterOption("陸前高田市", "陸前高田市", 3, 1, False),
        AreaFilterOption("陸前高田", "陸前高田", 1, 1, False),
        AreaFilterOption("東京都千代田区", "東京 / 千代田区", 2, 0, True),
        AreaFilterOption("東京", "東京", 2, 1, False),
    ]


def test_area_filter_same_name_cities_have_distinct_prefecture_headings() -> None:
    assert build_area_filter_options([("広島県府中市", 3), ("東京都府中市", 2)]) == [
        AreaFilterOption("東京都府中市", "東京 / 府中市", 2, 0, True),
        AreaFilterOption("広島県府中市", "広島 / 府中市", 3, 0, True),
    ]


def test_area_filter_non_municipality_headings_use_only_known_reserved_values() -> None:
    assert build_area_filter_options([("未分類B", 1), ("五島列島", 4), ("未分類A", 2)]) == [
        AreaFilterOption("__group__:長崎", "長崎", 4, 0, True),
        AreaFilterOption("五島列島", "五島列島", 4, 1, False),
        AreaFilterOption("__group__:その他", "その他", 3, 0, True),
        AreaFilterOption("未分類A", "未分類A", 2, 1, False),
        AreaFilterOption("未分類B", "未分類B", 1, 1, False),
    ]
    assert area_filter_matches("五島列島", "__group__:長崎")
    assert area_filter_matches("未分類A", "__group__:その他")
    assert not area_filter_matches("長崎県長崎市", "__group__:長崎")
    assert not area_filter_matches("門前仲町", "__group__:東京 / 江東区")
    assert not area_filter_matches("__group__:架空", "__group__:架空")
    assert not area_filter_matches("門前仲町", "__group__:その他")


def test_area_filter_options_match_counts_and_do_not_depend_on_bucket_order() -> None:
    counts = [
        ("東京都江東区", 2), ("門前仲町", 3), ("東京都江東区 / 木場", 1),
        ("神奈川県横浜市", 4), ("野毛", 2), ("神奈川県横浜市中区", 1),
        ("神奈川県横浜市西区", 3), ("祇園四条", 2), ("清水五条", 1),
        ("陸前高田市", 2), ("東京", 1), ("五島列島", 1), ("未分類", 1),
    ]
    options = build_area_filter_options(counts)
    assert options == build_area_filter_options(list(reversed(counts)))
    assert len({option.value for option in options}) == len(options)
    for option in options:
        assert option.count == sum(
            count for area, count in counts if area_filter_matches(area, option.value)
        )
    assert build_area_filter_options([]) == []


def test_all_official_city_wards_match_only_their_own_parent_city() -> None:
    wards = [row for row in MUNICIPALITIES if row.city]
    assert len(wards) == 171
    for ward in wards:
        assert area_filter_matches(ward.area, f"{ward.prefecture}{ward.city}")
        assert not area_filter_matches(f"{ward.prefecture}{ward.city}", ward.area)


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("東京都江東区", "東京 / 江東区"),
        ("東京都府中市", "東京 / 府中市"),
        ("広島県府中市", "広島 / 府中市"),
        ("__group__:その他", "その他"),
        ("__group__:長崎", "長崎"),
        ("__group__:架空", "__group__:架空"),
        ("東京都江東区 / 木場", "木場"),
        ("門前仲町", "門前仲町"),
    ],
)
def test_active_filter_labels_use_the_same_heading_or_leaf_name(value: str, label: str) -> None:
    assert area_filter_label(value) == label

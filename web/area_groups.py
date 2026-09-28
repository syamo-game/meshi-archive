from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from web.region_master import (
    MUNICIPALITIES,
    MUNICIPALITIES_BY_AREA,
    MUNICIPALITIES_BY_CODE,
    PREFECTURE_NAMES,
    TOKYO_STATIONS,
    Municipality,
    address_municipality,
    municipalities_in_text,
    municipality_address_tail,
    normalize_geography,
)


AREA_TO_GROUP: dict[str, str] = {
    "東京": "東京 / 千代田区",
    "東京駅": "東京 / 千代田区",
    "神田": "東京 / 千代田区",
    "秋葉原": "東京 / 千代田区",
    "丸の内": "東京 / 千代田区",
    "飯田橋": "東京 / 千代田区",
    "御茶ノ水": "東京 / 千代田区",
    "新御茶ノ水": "東京 / 千代田区",
    "有楽町": "東京 / 千代田区",
    "霞が関": "東京 / 千代田区",
    "銀座": "東京 / 中央区",
    "日本橋": "東京 / 中央区",
    "八重洲": "東京 / 中央区",
    "築地": "東京 / 中央区",
    "茅場町": "東京 / 中央区",
    "馬喰横山": "東京 / 中央区",
    "人形町": "東京 / 中央区",
    "勝どき": "東京 / 中央区",
    "月島": "東京 / 中央区",
    "新橋": "東京 / 港区",
    "虎ノ門": "東京 / 港区",
    "六本木": "東京 / 港区",
    "麻布十番": "東京 / 港区",
    "高輪": "東京 / 港区",
    "高輪台": "東京 / 港区",
    "高輪ゲートウェイ": "東京 / 港区",
    "白金": "東京 / 港区",
    "白金台": "東京 / 港区",
    "白金高輪": "東京 / 港区",
    "表参道": "東京 / 港区",
    "赤坂": "東京 / 港区",
    "溜池山王": "東京 / 港区",
    "乃木坂": "東京 / 港区",
    "三田": "東京 / 港区",
    "品川": "東京 / 港区",
    "大門": "東京 / 港区",
    "田町": "東京 / 港区",
    "西麻布": "東京 / 港区",
    "赤坂見附": "東京 / 港区",
    "新宿": "東京 / 新宿区",
    "新宿三丁目": "東京 / 新宿区",
    "西新宿": "東京 / 新宿区",
    "新大久保": "東京 / 新宿区",
    "四谷荒木町": "東京 / 新宿区",
    "四谷三丁目": "東京 / 新宿区",
    "白山": "東京 / 文京区",
    "上野": "東京 / 台東区",
    "上野広小路": "東京 / 台東区",
    "御徒町": "東京 / 台東区",
    "新御徒町": "東京 / 台東区",
    "浅草": "東京 / 台東区",
    "浅草橋": "東京 / 台東区",
    "蔵前": "東京 / 台東区",
    "湯島": "東京 / 文京区",
    "谷中": "東京 / 台東区",
    "錦糸町": "東京 / 墨田区",
    "押上": "東京 / 墨田区",
    "両国": "東京 / 墨田区",
    "菊川": "東京 / 墨田区",
    "曳舟": "東京 / 墨田区",
    "本所吾妻橋": "東京 / 墨田区",
    "清澄白河": "東京 / 江東区",
    "門前仲町": "東京 / 江東区",
    "森下": "東京 / 江東区",
    "亀戸": "東京 / 江東区",
    "南砂町": "東京 / 江東区",
    "枝川": "東京 / 江東区",
    "豊洲": "東京 / 江東区",
    "市場前": "東京 / 江東区",
    "住吉": "東京 / 江東区",
    "深川": "東京 / 江東区",
    "北砂": "東京 / 江東区",
    "新大橋": "東京 / 江東区",
    "東大島": "東京 / 江東区",
    "東陽町": "東京 / 江東区",
    "砂町銀座": "東京 / 江東区",
    "大井町": "東京 / 品川区",
    "大崎": "東京 / 品川区",
    "北品川": "東京 / 品川区",
    "旗の台": "東京 / 品川区",
    "中目黒": "東京 / 目黒区",
    "自由が丘": "東京 / 目黒区",
    "学芸大学": "東京 / 目黒区",
    "大森": "東京 / 大田区",
    "池上": "東京 / 大田区",
    "蒲田": "東京 / 大田区",
    "下北沢": "東京 / 世田谷区",
    "等々力": "東京 / 世田谷区",
    "経堂": "東京 / 世田谷区",
    "渋谷": "東京 / 渋谷区",
    "原宿": "東京 / 渋谷区",
    "恵比寿": "東京 / 渋谷区",
    "代々木上原": "東京 / 渋谷区",
    "中野": "東京 / 中野区",
    "東中野": "東京 / 中野区",
    "荻窪": "東京 / 杉並区",
    "阿佐ヶ谷": "東京 / 杉並区",
    "池袋": "東京 / 豊島区",
    "立石": "東京 / 葛飾区",
    "亀有": "東京 / 葛飾区",
    "小岩": "東京 / 江戸川区",
    "西葛西": "東京 / 江戸川区",
    "日暮里": "東京 / 荒川区",
    "吉祥寺": "東京 / 武蔵野市",
    "多摩センター": "東京 / 多摩市",
    "田無": "東京 / 西東京市",
    "札幌": "北海道",
    "函館": "北海道",
    "祝津": "北海道",
    "大間": "青森",
    "陸前高田": "岩手",
    "陸前高田市": "岩手",
    "塩釜": "宮城",
    "仙台": "宮城",
    "草加": "埼玉 / 草加市",
    "川越": "埼玉 / 川越市",
    "浦和": "埼玉 / さいたま市",
    "大宮": "埼玉 / さいたま市",
    "武蔵小杉": "神奈川 / 川崎市",
    "野毛": "神奈川 / 横浜市",
    "川崎": "神奈川 / 川崎市",
    "逗子": "神奈川 / 逗子市",
    "富山": "富山",
    "氷見": "富山",
    "金沢": "石川",
    "岐阜": "岐阜",
    "伊東": "静岡",
    "名古屋": "愛知",
    "稲沢": "愛知",
    "豊明": "愛知",
    "安城": "愛知",
    "知立": "愛知",
    "りんくう常滑": "愛知",
    "清水五条": "京都",
    "祇園四条": "京都",
    "芦屋": "兵庫",
    "徳島": "徳島",
    "高知": "高知",
    "五島列島": "長崎",
    "那覇空港": "沖縄",
}

AREA_ALIASES: dict[str, str] = {
    "中目黒駅": "中目黒",
    "森下三丁目": "森下",
    "学芸大学駅": "学芸大学",
    "門前仲町駅": "門前仲町",
    "初音小路": "谷中",
    "初音小路（谷中）": "谷中",
    "コレド室町": "日本橋",
    "コレド室町1": "日本橋",
    "東京虎ノ門": "虎ノ門",
    "東京都大田区池上": "池上",
    "山王健保会館": "赤坂",
    "上野御徒町": "御徒町",
    "菊川（東京都墨田区）": "菊川",
    "菊川三丁目": "菊川",
    "陸前高田市": "陸前高田",
}

_LEGACY_AREA_GROUPS: dict[str, str] = dict(AREA_TO_GROUP)
_LEGACY_LOCALITIES: dict[str, str] = {
    "札幌": "北海道札幌市",
    "函館": "北海道函館市",
    "祝津": "北海道小樽市",
    "大間": "青森県下北郡大間町",
    "陸前高田": "岩手県陸前高田市",
    "陸前高田市": "岩手県陸前高田市",
    "塩釜": "宮城県塩竈市",
    "仙台": "宮城県仙台市",
    "富山": "富山県富山市",
    "氷見": "富山県氷見市",
    "金沢": "石川県金沢市",
    "岐阜": "岐阜県岐阜市",
    "伊東": "静岡県伊東市",
    "名古屋": "愛知県名古屋市",
    "稲沢": "愛知県稲沢市",
    "豊明": "愛知県豊明市",
    "安城": "愛知県安城市",
    "知立": "愛知県知立市",
    "りんくう常滑": "愛知県常滑市",
    "清水五条": "京都府京都市東山区",
    "祇園四条": "京都府京都市東山区",
    "芦屋": "兵庫県芦屋市",
    "徳島": "徳島県徳島市",
    "高知": "高知県高知市",
    "那覇空港": "沖縄県那覇市",
}
_AREA_MUNICIPALITIES: dict[str, Municipality] = {}
_DISPLAY_LABELS: dict[str, str] = {}
_ALIAS_CANDIDATES: dict[str, set[str]] = {}


def _add_alias(alias: str, area: str) -> None:
    _ALIAS_CANDIDATES.setdefault(normalize_geography(alias), set()).add(area)


for _municipality in MUNICIPALITIES:
    _area = _municipality.area
    AREA_TO_GROUP[_area] = f"{_municipality.region} / {_municipality.name}"
    _AREA_MUNICIPALITIES[_area] = _municipality
    _DISPLAY_LABELS[_area] = _municipality.name
    _add_alias(_area, _area)
    for _alias in (*_municipality.aliases, _municipality.municipality):
        _add_alias(_alias, _area)
        _add_alias(f"{_municipality.prefecture}{_alias}", _area)
        _add_alias(f"{_municipality.region} / {_alias}", _area)

for _area, _group in _LEGACY_AREA_GROUPS.items():
    _parent_name = _LEGACY_LOCALITIES.get(_area)
    if _parent_name is None and " / " in _group:
        _region, _parent = _group.split(" / ", 1)
        _parent_name = f"{PREFECTURE_NAMES[_region]}{_parent}"
    if _parent_name is not None:
        _municipality = MUNICIPALITIES_BY_AREA.get(_parent_name)
        if _municipality is None:
            raise ValueError(f"Legacy area has no municipality: {_area}: {_parent_name}")
        _AREA_MUNICIPALITIES[_area] = _municipality
        AREA_TO_GROUP[_area] = f"{_municipality.region} / {_municipality.name}"
        _add_alias(f"{_municipality.area}{_area}", _area)
        _add_alias(f"{_municipality.area} / {_area}", _area)
        _add_alias(f"{_municipality.area}{_area}", _area)
        _add_alias(f"{_municipality.area} / {_area}", _area)

for _station in TOKYO_STATIONS:
    _municipality = MUNICIPALITIES_BY_CODE[_station.municipality_code]
    _station_label = "東京駅" if _station.station_name == "東京" else _station.station_name
    _legacy_parent = _AREA_MUNICIPALITIES.get(_station_label)
    if _station_label in _LEGACY_AREA_GROUPS and _legacy_parent == _municipality:
        _area = _station_label
    else:
        _area = f"{_municipality.area} / {_station.station_name}"
        AREA_TO_GROUP[_area] = f"東京 / {_municipality.name}"
        _AREA_MUNICIPALITIES[_area] = _municipality
        _DISPLAY_LABELS[_area] = _station.station_name
    _add_alias(_area, _area)
    _add_alias(_station.station_name, _area)
    _add_alias(f"{_station.station_name}駅", _area)
    _add_alias(f"{_municipality.area}{_station.station_name}", _area)
    _add_alias(f"{_municipality.area}{_station.station_name}駅", _area)
    _add_alias(f"{_municipality.area} / {_station.station_name}", _area)
    _add_alias(f"{_municipality.area} / {_station.station_name}駅", _area)
    _add_alias(f"{_municipality.area} / {_station.station_name}", _area)
    _add_alias(f"{_municipality.area} / {_station.station_name}駅", _area)
    _add_alias(f"{_municipality.name} / {_station.station_name}", _area)

GROUPS_IN_ORDER: list[str] = [f"{row.region} / {row.name}" for row in MUNICIPALITIES]
GROUPS_IN_ORDER.extend(group for group in _LEGACY_AREA_GROUPS.values() if group not in GROUPS_IN_ORDER)
GROUPS_IN_ORDER.append("その他")
_GROUP_ORDER_INDEX: dict[str, int] = {
    group: index for index, group in enumerate(GROUPS_IN_ORDER)
}
_NON_MUNICIPALITY_FILTER_GROUPS: frozenset[str] = frozenset(GROUPS_IN_ORDER) - {
    AREA_TO_GROUP[row.area] for row in MUNICIPALITIES
}
_FILTER_GROUP_PREFIX = "__group__:"

NON_CANONICAL_DISPLAY_AREAS: frozenset[str] = frozenset({"東京", "陸前高田市"})
CANONICAL_AREAS: frozenset[str] = (
    frozenset(AREA_TO_GROUP) - NON_CANONICAL_DISPLAY_AREAS
)

_NORMALIZED_AREA_ALIASES: dict[str, str] = {
    unicodedata.normalize("NFKC", alias).strip(): canonical
    for alias, canonical in AREA_ALIASES.items()
}


def canonicalize_area(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return None
    canonical = _NORMALIZED_AREA_ALIASES.get(normalized, normalized)
    if canonical in CANONICAL_AREAS:
        return canonical
    if normalized in NON_CANONICAL_DISPLAY_AREAS:
        return None
    candidates = _ALIAS_CANDIDATES.get(normalize_geography(normalized), set())
    return next(iter(candidates)) if len(candidates) == 1 else None


def area_municipality(area: str) -> Municipality | None:
    canonical = canonicalize_area(area)
    return _AREA_MUNICIPALITIES.get(canonical) if canonical else None


def area_is_municipality(area: str) -> bool:
    return canonicalize_area(area) in MUNICIPALITIES_BY_AREA


def area_display_label(area: str) -> str:
    return _DISPLAY_LABELS.get(area, area)


def area_filter_label(area: str) -> str:
    """Use the same heading in the selector and active filters."""
    if area in MUNICIPALITIES_BY_AREA:
        return AREA_TO_GROUP[area]
    if area.startswith(_FILTER_GROUP_PREFIX):
        group = area.removeprefix(_FILTER_GROUP_PREFIX)
        if group in _NON_MUNICIPALITY_FILTER_GROUPS:
            return group
    return area if area_is_municipality(area) else area_display_label(area)


@dataclass(frozen=True)
class AreaFilterOption:
    value: str
    label: str
    count: int
    depth: int
    is_group: bool


def area_filter_matches(stored_area: str, selected_area: str) -> bool:
    """Match headings by membership and individual areas by their saved value."""
    selected_municipality = MUNICIPALITIES_BY_AREA.get(selected_area)
    canonical = canonicalize_area(stored_area) or stored_area
    if selected_municipality is not None:
        stored_municipality = _AREA_MUNICIPALITIES.get(canonical)
        if stored_municipality is None:
            return False
        return stored_municipality == selected_municipality or (
            not selected_municipality.city
            and bool(stored_municipality.city)
            and stored_municipality.prefecture_code == selected_municipality.prefecture_code
            and stored_municipality.city == selected_municipality.municipality
        )
    if selected_area.startswith(_FILTER_GROUP_PREFIX):
        group = selected_area.removeprefix(_FILTER_GROUP_PREFIX)
        return (
            group in _NON_MUNICIPALITY_FILTER_GROUPS
            and AREA_TO_GROUP.get(canonical, "その他") == group
        )
    return stored_area == selected_area


def build_area_filter_options(
    areas_with_counts: list[tuple[str, int]],
) -> list[AreaFilterOption]:
    counts: dict[str, int] = {}
    leaves: dict[str, dict[str, int]] = {}
    parents: dict[str, str] = {}
    children: dict[str, set[str]] = {}
    for area, count in areas_with_counts:
        canonical = canonicalize_area(area) or area
        municipality = _AREA_MUNICIPALITIES.get(canonical)
        value = municipality.area if municipality else (
            f"{_FILTER_GROUP_PREFIX}{AREA_TO_GROUP.get(canonical, 'その他')}"
        )
        counts[value] = counts.get(value, 0) + count
        if area != value:
            group_leaves = leaves.setdefault(value, {})
            group_leaves[area] = group_leaves.get(area, 0) + count
        if municipality is not None and municipality.city:
            city = MUNICIPALITIES_BY_AREA[f"{municipality.prefecture}{municipality.city}"]
            parents[value] = city.area
            children.setdefault(city.area, set()).add(value)
            # Add each saved bucket once, rather than summing overlapping groups.
            counts[city.area] = counts.get(city.area, 0) + count

    def order_key(value: str) -> tuple[int, str]:
        label = area_filter_label(value)
        return (_GROUP_ORDER_INDEX.get(label, len(GROUPS_IN_ORDER)), label)

    result: list[AreaFilterOption] = []

    def append_group(value: str, depth: int) -> None:
        result.append(AreaFilterOption(value, area_filter_label(value), counts[value], depth, True))
        for area, count in sorted(leaves.get(value, {}).items(), key=lambda item: (-item[1], item[0])):
            result.append(AreaFilterOption(area, area_filter_label(area), count, depth + 1, False))
        for child in sorted(children.get(value, set()), key=order_key):
            append_group(child, depth + 1)

    for value in sorted(counts.keys() - parents.keys(), key=order_key):
        append_group(value, 0)
    return result


def area_list_sort_key(area: str | None) -> tuple[int, int, str]:
    """Keep Tokyo first and use the same municipality order as the area list."""
    if not area or not area.strip():
        return (3, 0, "")
    canonical = canonicalize_area(area) or area
    group = AREA_TO_GROUP.get(canonical)
    if group is None:
        return (2, 0, area)
    return (
        0 if group.startswith("東京 / ") else 1,
        _GROUP_ORDER_INDEX[group],
        area_display_label(canonical),
    )


def editable_area_groups() -> list[tuple[str, list[tuple[str, int]]]]:
    buckets: dict[str, list[tuple[str, int]]] = {}
    for row in MUNICIPALITIES:
        group = f"東京 / {row.name}" if row.prefecture_code == "13" else row.region
        buckets.setdefault(group, []).append((row.area, 0))
    for area in sorted(CANONICAL_AREAS - MUNICIPALITIES_BY_AREA.keys(), key=area_display_label):
        parent = _AREA_MUNICIPALITIES.get(area)
        group = AREA_TO_GROUP[area] if parent and parent.prefecture_code == "13" else AREA_TO_GROUP[area].split(" / ")[0]
        buckets.setdefault(group, []).append((area, 0))
    return list(buckets.items())


_LEAF_AREAS_BY_MUNICIPALITY: dict[str, dict[str, str]] = {}
for _area, _municipality in _AREA_MUNICIPALITIES.items():
    if _area in CANONICAL_AREAS and _area not in MUNICIPALITIES_BY_AREA:
        _LEAF_AREAS_BY_MUNICIPALITY.setdefault(_municipality.code, {})[_area] = normalize_geography(area_display_label(_area))


def area_from_address(value: str) -> str | None:
    parent = address_municipality(value)
    if parent is None:
        return None
    remainder = municipality_address_tail(value, parent)
    if remainder is None:
        return None
    leaves = _LEAF_AREAS_BY_MUNICIPALITY.get(parent.code, {})
    matches = [(area, token) for area, token in leaves.items() if remainder.startswith(token)]
    if matches:
        longest = max(len(token) for _, token in matches)
        areas = {area for area, token in matches if len(token) == longest}
        if len(areas) == 1:
            return next(iter(areas))
    return parent.area


def is_known_area(value: str | None) -> bool:
    return canonicalize_area(value) is not None


def area_ward(area: str) -> str | None:
    municipality = _AREA_MUNICIPALITIES.get(area)
    if municipality is not None and area in MUNICIPALITIES_BY_AREA:
        return municipality.prefecture
    if municipality is not None and " / " in area:
        return municipality.name if municipality.prefecture_code == "13" else municipality.area
    group = AREA_TO_GROUP.get(area)
    if not group:
        return None
    parent = group.split(" / ", 1)[1] if " / " in group else group
    return parent if parent != area else None


def group_areas(
    areas_with_counts: list[tuple[str, int]],
) -> list[tuple[str, list[tuple[str, int]]]]:
    buckets: dict[str, list[tuple[str, int]]] = {}
    for area, count in areas_with_counts:
        group = AREA_TO_GROUP.get(area, "その他")
        buckets.setdefault(group, []).append((area, count))

    # Do not trust source order because area labels arrive from external data.
    order_index = {label: i for i, label in enumerate(GROUPS_IN_ORDER)}
    fallback_index = len(GROUPS_IN_ORDER)

    result: list[tuple[str, list[tuple[str, int]]]] = []
    for group, items in sorted(
        buckets.items(),
        key=lambda kv: (order_index.get(kv[0], fallback_index), kv[0]),
    ):
        items.sort(key=lambda ac: (-ac[1], ac[0]))
        result.append((group, items))
    return result

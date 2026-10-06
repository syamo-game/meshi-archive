from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Municipality:
    code: str
    prefecture_code: str
    prefecture: str
    county: str
    city: str
    municipality: str

    @property
    def name(self) -> str:
        return f"{self.county}{self.city}{self.municipality}"

    @property
    def region(self) -> str:
        return self.prefecture if self.prefecture == "北海道" else self.prefecture[:-1]

    @property
    def area(self) -> str:
        return f"{self.prefecture}{self.name}"

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.name, f"{self.city}{self.municipality}")))


@dataclass(frozen=True)
class Station:
    station_name: str
    municipality_code: str
    source_station_id: str


_DATA_DIRECTORY = Path(__file__).with_name("data")


def _load_municipalities() -> tuple[Municipality, ...]:
    path = _DATA_DIRECTORY / "municipalities.csv"
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        expected = {"code", "prefecture_code", "prefecture", "county", "city", "municipality"}
        if set(reader.fieldnames or ()) != expected:
            raise ValueError(f"Invalid municipality columns in {path}: {reader.fieldnames}")
        rows = tuple(Municipality(**row) for row in reader)
    codes: set[str] = set()
    for row in rows:
        if (
            not re.fullmatch(r"\d{6}", row.code)
            or row.prefecture_code != row.code[:2]
            or not row.prefecture
            or not row.municipality
            or row.code in codes
        ):
            raise ValueError(f"Invalid or duplicate municipality in {path}: {row}")
        codes.add(row.code)
    if len({row.prefecture_code for row in rows}) != 47:
        raise ValueError(f"Municipality master must cover 47 prefectures: {path}")
    if sum(row.prefecture_code == "13" for row in rows) != 62:
        raise ValueError(f"Municipality master must cover all 62 Tokyo municipalities: {path}")
    return tuple(sorted(rows, key=lambda row: row.code))


MUNICIPALITIES: tuple[Municipality, ...] = _load_municipalities()
MUNICIPALITIES_BY_CODE: dict[str, Municipality] = {row.code: row for row in MUNICIPALITIES}
MUNICIPALITIES_BY_AREA: dict[str, Municipality] = {row.area: row for row in MUNICIPALITIES}
PREFECTURE_NAMES: dict[str, str] = {row.region: row.prefecture for row in MUNICIPALITIES}
PREFECTURES_BY_CODE: dict[str, str] = {row.prefecture_code: row.prefecture for row in MUNICIPALITIES}


def _load_stations() -> tuple[Station, ...]:
    path = _DATA_DIRECTORY / "tokyo_stations.csv"
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        expected = {"station_name", "municipality_code", "source_station_id"}
        if set(reader.fieldnames or ()) != expected:
            raise ValueError(f"Invalid station columns in {path}: {reader.fieldnames}")
        rows = tuple(Station(**row) for row in reader)
    for row in rows:
        parent = MUNICIPALITIES_BY_CODE.get(row.municipality_code)
        if not parent or parent.prefecture_code != "13" or not row.station_name:
            raise ValueError(f"Invalid Tokyo station in {path}: {row}")
    if not rows:
        raise ValueError(f"Tokyo station master is empty: {path}")
    return rows


TOKYO_STATIONS: tuple[Station, ...] = _load_stations()


def normalize_geography(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


_PREFECTURE_PATTERN = re.compile("|".join(map(re.escape, PREFECTURES_BY_CODE.values())))
_LOCALITY_OPTIONS: dict[str, tuple[tuple[str, Municipality], ...]] = {
    prefecture: tuple(
        sorted(
            (
                (normalize_geography(alias), row)
                for row in MUNICIPALITIES
                if row.prefecture == prefecture
                for alias in row.aliases
            ),
            key=lambda item: (-len(item[0]), item[1].code),
        )
    )
    for prefecture in PREFECTURES_BY_CODE.values()
}


def municipalities_in_text(value: str) -> tuple[Municipality, ...]:
    """Read address prefixes; do not treat later street names as municipalities."""
    normalized = normalize_geography(value)
    prefectures = tuple(_PREFECTURE_PATTERN.finditer(normalized))
    chunks: list[tuple[str, str]] = []
    if prefectures:
        for index, match in enumerate(prefectures):
            end = prefectures[index + 1].start() if index + 1 < len(prefectures) else len(normalized)
            chunks.append((match.group(), normalized[match.end():end]))
    else:
        chunks.extend((prefecture, normalized) for prefecture in PREFECTURES_BY_CODE.values())
    found: dict[str, Municipality] = {}
    for prefecture, chunk in chunks:
        for part in re.split(r"[/／、,;；|｜]", chunk):
            part = part.lstrip(" :：・")
            matches = tuple((alias, row) for alias, row in _LOCALITY_OPTIONS[prefecture] if part.startswith(alias))
            if not matches:
                continue
            longest = len(matches[0][0])
            for alias, row in matches:
                if len(alias) == longest:
                    found[row.code] = row
    return tuple(found[code] for code in sorted(found))


def address_municipality(value: str) -> Municipality | None:
    normalized = normalize_geography(value)
    if len(tuple(_PREFECTURE_PATTERN.finditer(normalized))) != 1:
        return None
    matches = municipalities_in_text(value)
    return matches[0] if len(matches) == 1 else None


def municipality_address_tail(value: str, municipality: Municipality) -> str | None:
    normalized = normalize_geography(value)
    start = normalized.find(municipality.prefecture)
    if start < 0:
        return None
    chunk = normalized[start + len(municipality.prefecture):]
    for part in re.split(r"[/／、,;；|｜]", chunk):
        part = part.lstrip(" :：・")
        aliases = [alias for alias in municipality.aliases if part.startswith(alias)]
        if aliases:
            return part[len(max(aliases, key=len)):]
    return None

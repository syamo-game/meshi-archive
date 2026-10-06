"""Build region CSV snapshots from official Japanese open data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.request import urlopen
from zipfile import ZipFile

from pydantic import BaseModel
from shapely import STRtree, from_geojson
from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class Source:
    filename: str
    url: str
    basis_date: str
    sha256: str
    license_url: str


SOURCES: tuple[Source, ...] = (
    Source(
        "mt_city_all.csv.zip",
        "https://data.address-br.digital.go.jp/mt_city/mt_city_all.csv.zip",
        "2024-03-12 (date listed in the official catalog)",
        "c719e7394489907c6192f58837d7f61274c05feb72ebec2543995d0d6cb08b5e",
        "https://www.digital.go.jp/policies/base_registry_address_tos",
    ),
    Source(
        "N02-25_GML.zip",
        "https://nlftp.mlit.go.jp/ksj/gml/data/N02/N02-25/N02-25_GML.zip",
        "2025-12-31",
        "aaf76af133b2e771e538fabc4646d2e443dc1d5a67b221382a28d744e706cc9f",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
    Source(
        "N03-20260101_13_GML.zip",
        "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2026/N03-20260101_13_GML.zip",
        "2026-01-01",
        "94f10b26256566db970dd74b09d614f059c1e8a432f9244ac9c4add76c32ff16",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
)


@dataclass(frozen=True)
class Municipality:
    code: str
    prefecture_code: str
    prefecture: str
    county: str
    city: str
    municipality: str


@dataclass(frozen=True)
class Station:
    station_name: str
    municipality_code: str
    source_station_id: str


class LineGeometry(BaseModel):
    type: Literal["LineString"]
    coordinates: list[tuple[float, float]]


class BoundaryGeometry(BaseModel):
    type: Literal["MultiPolygon"]
    coordinates: list[list[list[tuple[float, float]]]]


class StationProperties(BaseModel):
    N02_005: str
    N02_005c: str


class BoundaryProperties(BaseModel):
    N03_004: str
    N03_007: str


class StationFeature(BaseModel):
    type: Literal["Feature"]
    properties: StationProperties
    geometry: LineGeometry


class BoundaryFeature(BaseModel):
    type: Literal["Feature"]
    properties: BoundaryProperties
    geometry: BoundaryGeometry


class StationCollection(BaseModel):
    type: Literal["FeatureCollection"]
    features: list[StationFeature]


class BoundaryCollection(BaseModel):
    type: Literal["FeatureCollection"]
    features: list[BoundaryFeature]


def read_archive(source: Source, source_dir: Path, download: bool) -> ZipFile:
    archive_path: Path = source_dir / source.filename
    if download:
        source_dir.mkdir(parents=True, exist_ok=True)
        with urlopen(source.url, timeout=120) as response:
            archive_path.write_bytes(response.read())
    archive_bytes: bytes = archive_path.read_bytes()
    actual_hash: str = hashlib.sha256(archive_bytes).hexdigest()
    if actual_hash != source.sha256:
        raise ValueError(
            f"Source checksum changed: url={source.url}, "
            f"expected={source.sha256}, actual={actual_hash}. "
            "Review the new source before updating the pinned checksum."
        )
    return ZipFile(io.BytesIO(archive_bytes))


def load_municipalities(archive: ZipFile) -> list[Municipality]:
    text: str = archive.read("mt_city_all.csv").decode("utf-8-sig")
    municipalities: list[Municipality] = []
    for row in csv.DictReader(io.StringIO(text)):
        code: str = row["lg_code"]
        if row["ablt_date"]:
            raise ValueError(f"Unexpected abolished municipality: code={code}")
        if len(code) != 6 or not code.isdigit() or not row["city"]:
            raise ValueError(f"Invalid municipality source row: code={code}")
        municipalities.append(
            Municipality(
                code=code,
                prefecture_code=code[:2],
                prefecture=row["pref"],
                county=row["county"],
                city=row["city"] if row["ward"] else "",
                municipality=row["ward"] or row["city"],
            )
        )
    if len({item.code for item in municipalities}) != len(municipalities):
        raise ValueError("Duplicate municipality codes in the official master")
    if len({item.prefecture_code for item in municipalities}) != 47:
        raise ValueError("The municipality source does not cover 47 prefectures")
    if sum(item.prefecture_code == "13" for item in municipalities) != 62:
        raise ValueError("The municipality source does not cover all 62 Tokyo municipalities")
    return sorted(municipalities, key=lambda item: item.code)


def load_stations(
    station_archive: ZipFile,
    boundary_archive: ZipFile,
    municipalities: list[Municipality],
) -> list[Station]:
    station_data: StationCollection = StationCollection.model_validate_json(
        station_archive.read("N02-25_GML/UTF-8/N02-25_Station.geojson")
    )
    boundary_data: BoundaryCollection = BoundaryCollection.model_validate_json(
        boundary_archive.read("N03-20260101_13.geojson")
    )
    tokyo_codes: dict[str, str] = {
        item.code[:5]: item.code
        for item in municipalities
        if item.prefecture_code == "13"
    }
    boundary_geometries: list[BaseGeometry] = []
    boundary_codes: list[str] = []
    for feature in boundary_data.features:
        code: str = feature.properties.N03_007
        if code == "13000":
            # The official source labels this reclaimed land outside municipalities.
            continue
        if code not in tokyo_codes:
            raise ValueError(f"Tokyo boundary has an unknown municipality: code={code}")
        geometry: BaseGeometry = from_geojson(feature.geometry.model_dump_json())
        if not geometry.is_valid:
            raise ValueError(f"Invalid official boundary geometry: code={code}")
        boundary_geometries.append(geometry)
        boundary_codes.append(tokyo_codes[code])
    tree: STRtree = STRtree(boundary_geometries)
    grouped_ids: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for feature in station_data.features:
        geometry = from_geojson(feature.geometry.model_dump_json())
        for index_value in tree.query(geometry, predicate="intersects"):
            index: int = int(index_value)
            # Only actual line overlap counts; touching at one point does not.
            if geometry.intersection(boundary_geometries[index]).length <= 0:
                continue
            key: tuple[str, str] = (
                feature.properties.N02_005,
                boundary_codes[index],
            )
            grouped_ids[key].add(feature.properties.N02_005c)
    if not grouped_ids:
        raise ValueError("No Tokyo stations matched the official administrative boundaries")
    return [
        Station(name, code, ";".join(sorted(source_ids)))
        for (name, code), source_ids in sorted(
            grouped_ids.items(), key=lambda item: (item[0][1], item[0][0])
        )
    ]


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("web/data"))
    parser.add_argument("--download", action="store_true")
    args: argparse.Namespace = parser.parse_args()
    source_dir: Path = args.source_dir
    output_dir: Path = args.output_dir
    download: bool = args.download
    with (
        read_archive(SOURCES[0], source_dir, download) as municipality_archive,
        read_archive(SOURCES[1], source_dir, download) as station_archive,
        read_archive(SOURCES[2], source_dir, download) as boundary_archive,
    ):
        municipalities: list[Municipality] = load_municipalities(municipality_archive)
        stations: list[Station] = load_stations(
            station_archive, boundary_archive, municipalities
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "municipalities.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("code", "prefecture_code", "prefecture", "county", "city", "municipality"))
        for item in municipalities:
            writer.writerow((item.code, item.prefecture_code, item.prefecture, item.county, item.city, item.municipality))
    with (output_dir / "tokyo_stations.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("station_name", "municipality_code", "source_station_id"))
        for station in stations:
            writer.writerow((station.station_name, station.municipality_code, station.source_station_id))
    print(f"Wrote {len(municipalities)} municipalities and {len(stations)} station/municipality pairs")


if __name__ == "__main__":
    main()

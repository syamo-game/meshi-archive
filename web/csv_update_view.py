"""Present CSV updates with shop names and readable field values."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Shop
from services.import_service import CSV_UPDATE_COLUMNS, ImportPreview


COLUMN_LABELS: dict[str, str] = {
    "shop.name": "店名", "shop.branch_name": "支店", "shop.area": "エリア",
    "shop.category": "カテゴリ", "shop.address": "住所", "shop.phone": "電話番号",
    "canonical_url": "店舗URL", "status.is_visited": "訪問状況", "visited_at": "訪問日",
    "rating": "評価", "memo": "メモ",
}
_FIELD_LABELS: dict[str, str] = {
    **{field: COLUMN_LABELS[column] for column, field in CSV_UPDATE_COLUMNS.items()},
    **COLUMN_LABELS, "_id": "店舗の識別情報", "shop_id": "店舗の識別情報",
    "shop.version": "更新確認用の情報", "shop_version": "更新確認用の情報",
    "message_id": "投稿の識別情報",
}
_FIELD_GUIDES: dict[str, str] = {
    "shop_name": "店名は1〜500文字で入力してください。",
    "branch_name": "支店は255文字以内で入力してください。",
    "area": "エリアは登録済みの市区町村名・駅名を入力してください。",
    "category": "カテゴリは登録内容の確認画面で使う分類を入力してください。",
    "address": "住所は2,000文字以内で入力してください。",
    "phone": "電話番号は32文字以内で入力してください。",
    "canonical_url": "店舗URLはhttpまたはhttpsで始まるURLを入力してください。",
    "is_visited": "訪問状況はtrue（訪問済み）またはfalse（未訪問）で入力してください。",
    "visited_at": "訪問日はYYYY-MM-DD形式で入力してください。",
    "rating": "評価は1〜5の整数で入力してください。",
    "memo": "メモは20,000文字以内で入力してください。",
    "shop_id": "編集用CSVを取り直し、店舗情報の列だけを編集してください。",
    "shop_version": "編集用CSVを取り直し、店舗情報の列だけを編集してください。",
    "message_id": "編集用CSVを取り直し、投稿の識別情報は変更しないでください。",
}
_ROW = re.compile(r"^(\d+)行目:\s*")
_FIELD_PATTERN = re.compile(
    r"(?<![\w.])(" + "|".join(re.escape(key) for key in sorted(_FIELD_LABELS, key=len, reverse=True)) + r")(?![\w.])"
)


@dataclass(frozen=True)
class CsvChangeView:
    shop_id: int
    shop_name: str
    field_label: str
    previous: str
    proposed: str


def _display_value(column: str, value: str) -> str:
    if not value:
        return "（空欄）"
    if column == "status.is_visited":
        if value not in {"True", "False"}:
            raise ValueError(f"Invalid visit state in CSV preview: {value}")
        return "訪問済み" if value == "True" else "未訪問"
    if column == "rating":
        return f"{value} / 5"
    if column == "visited_at":
        return value.replace("-", "/", 2).replace("T", " ")
    return value


def csv_change_views(db: Session, preview: ImportPreview) -> tuple[CsvChangeView, ...]:
    shop_ids: set[int] = {change.shop_id for change in preview.changes}
    names: dict[int, str] = {}
    if shop_ids:
        for shop_id, name, branch in db.execute(
            select(Shop.id, Shop.shop_name, Shop.branch_name).where(Shop.id.in_(shop_ids))
        ):
            names[shop_id] = f"{name} {branch}" if branch else name
    rows: list[CsvChangeView] = []
    for change in preview.changes:
        if change.shop_id not in names or change.column not in COLUMN_LABELS:
            raise ValueError(f"Missing CSV preview presentation: shop_id={change.shop_id} column={change.column}")
        rows.append(CsvChangeView(
            shop_id=change.shop_id, shop_name=names[change.shop_id], field_label=COLUMN_LABELS[change.column],
            previous=_display_value(change.column, change.previous),
            proposed=_display_value(change.column, change.proposed),
        ))
    return tuple(rows)


def csv_error_messages(errors: list[str]) -> list[str]:
    messages: list[str] = []
    field_by_column: dict[str, str] = {**{column: field for column, field in CSV_UPDATE_COLUMNS.items()}}
    field_by_column.update({"_id": "shop_id", "shop.version": "shop_version"})
    for message in errors:
        row_match = _ROW.match(message)
        prefix: str = f"{row_match.group(1)}行目：" if row_match else ""
        if "更新済み" in message:
            messages.append(prefix + "この店舗は更新済みです。最新のCSVをダウンロードして編集し直してください。")
        elif "_id=" in message and "関連が一致しません" in message:
            messages.append(prefix + "店舗と投稿の識別情報が一致しません。編集用CSVを取り直して店舗情報だけを編集してください。")
        elif "_id=" in message and "存在しません" in message:
            messages.append(prefix + "この店舗が見つかりません。最新のCSVをダウンロードしてください。")
        elif "必須列がありません" in message or "_id と shop.version" in message:
            messages.append(prefix + "店舗の識別情報が不足しているか、変更されています。最新のCSVをダウンロードして編集し直してください。")
        elif "_id=" in message and "重複" in message:
            messages.append(prefix + "同じ店舗の行が重複しています。1店舗につき1行にしてください。")
        elif "_id=" in message and "全行の更新を取り消しました" in message:
            messages.append(prefix + "この店舗が変更されたため、保存を取り消しました。最新のCSVをダウンロードして確認し直してください。")
        elif "status.is_visited=false" in message:
            messages.append(prefix + "未訪問の店舗には訪問日を設定できません。訪問済みにするか、訪問日を空欄にしてください。")
        elif "CSV形式が不正です" in message:
            messages.append("CSVの形式を読み取れません。編集したファイルをCSV形式で保存し直してください。")
        elif "検証したCSVと一致しません" in message:
            messages.append("確認したCSVと一致しません。戻って、変更内容を確認し直してください。")
        elif re.search(r"must |cannot |unsupported datetime|Value error|Input should|String should|URL must|URL has|URL userinfo", message):
            keys: list[str] = list(dict.fromkeys(match.group(1) for match in _FIELD_PATTERN.finditer(message)))
            if not keys:
                raise ValueError(f"Unrecognized CSV validation message: {message}")
            for key in keys:
                field: str = field_by_column.get(key, key)
                messages.append(prefix + _FIELD_GUIDES[field])
        else:
            messages.append(_FIELD_PATTERN.sub(lambda match: _FIELD_LABELS[match.group(1)], message))
    return messages

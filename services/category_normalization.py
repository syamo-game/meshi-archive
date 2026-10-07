from __future__ import annotations


CATEGORY_VALUES: tuple[str, ...] = (
    "寿司・回転寿司",
    "海鮮・刺身",
    "うなぎ",
    "天ぷら",
    "とんかつ・揚げ物",
    "焼き鳥・串焼き",
    "すき焼き",
    "しゃぶしゃぶ",
    "そば",
    "うどん",
    "お好み焼き・たこ焼き",
    "丼・定食",
    "おでん",
    "和食・日本料理",
    "割烹",
    "郷土料理・沖縄料理",
    "洋食・ハンバーグ",
    "ステーキ・鉄板焼き",
    "フレンチ・ビストロ",
    "イタリアン・パスタ・ピザ",
    "スペイン料理",
    "アメリカ料理・ハンバーガー",
    "中華料理",
    "台湾料理",
    "飲茶・点心",
    "餃子",
    "韓国料理",
    "タイ料理",
    "ベトナム料理",
    "インド料理",
    "カレー",
    "スープカレー",
    "エスニック料理",
    "焼肉",
    "ホルモン",
    "ジンギスカン",
    "鍋・もつ鍋",
    "居酒屋",
    "ダイニングバー",
    "立ち飲み・バル",
    "ビアガーデン・ビアホール",
    "ラーメン",
    "つけ麺",
    "担々麺・油そば",
    "カフェ・喫茶店",
    "甘味処",
    "スイーツ・洋菓子",
    "パン・ベーカリー",
    "バー・ワインバー",
    "弁当・惣菜・デリ",
    "ビュッフェ",
    "創作料理・イノベーティブ",
    "その他",
)
_CATEGORY_SET = frozenset(CATEGORY_VALUES)
_CATEGORY_ALIASES: dict[str, str] = {
    "ジェラート": "スイーツ・洋菓子",
    "ジェラート専門店": "スイーツ・洋菓子",
    "アイスクリーム": "スイーツ・洋菓子",
    "ソフトクリーム": "スイーツ・洋菓子",
}


def is_known_category(value: str | None) -> bool:
    return bool(value and value in _CATEGORY_SET)


def canonicalize_category(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if is_known_category(cleaned):
        return cleaned
    # Substring matches could incorrectly approve mixed or uncertain categories.
    return _CATEGORY_ALIASES.get(cleaned)

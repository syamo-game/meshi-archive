import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Initialize OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
else:
    logger.warning("OPENAI_API_KEY is not set.")
    openai_client = None

# ---------------------------------------------------------------------------
# Step 1: Extract basic info from Discord text / embeds
# ---------------------------------------------------------------------------
STEP1_SYSTEM_PROMPT = """
あなたはDiscordに投稿されたテキストやURL、埋め込み(Embed)情報から、飲食店の情報を抽出するアシスタントです。
必ず指定されたJSONフォーマットでのみ回答してください。

# 判定ルール
- 外食・テイクアウト・デリバリー・お取り寄せ・スーパーやデパ地下の弁当・惣菜など、食べ物やお店の情報であればすべて対象（ignore: false）。
- Youtubeのグルメ動画、食べログ・X(Twitter)などのリンクが含まれる場合も、本文・タイトルから店舗情報が読み取れれば対象。
- 単なる会話・相槌、または食べ物と無関係な話題のみの場合は対象外（ignore: true）。

# 抽出ルール（ignore: false の場合）
- 1つのメッセージに複数の店舗が含まれる場合は、すべてを shops 配列に含める。
- `shop_name`: 単一店舗として識別できる正式店舗名。チェーン名のみ・施設名のみ・人物名・SNS名・YouTube名の場合は null。
- `area`: 最寄り駅名・街区・街レベルの地名（例: 浅草、神田、荻窪、蔵前）。都道府県名・区全体・商業施設名だけの場合は null。本文やURL内の文字列から読み取れる場合のみ入れる。
- `category`: 店の主業態・料理ジャンル（例: 寿司、ラーメン、居酒屋、焼肉、カフェ、ベーカリー）。メニュー名・食材名・看板商品名は不可。不明な場合は null。
- `url`: 本文中に含まれるURL（食べログ・YouTube・X等）。複数ある場合はその店舗情報に最も近いものを1つ。なければ null。

# 出力フォーマット（対象の場合）
{
    "ignore": false,
    "shops": [
        { "shop_name": "天よし", "area": "浅草", "category": "天ぷら", "url": "https://..." },
        { "shop_name": "神田まつや", "area": "神田", "category": "そば", "url": null }
    ]
}

# 出力フォーマット（対象外の場合）
{
    "ignore": true
}
"""

# ---------------------------------------------------------------------------
# Step 2: Enrich a single shop using its URL page content
# ---------------------------------------------------------------------------
STEP2_SYSTEM_PROMPT = """
あなたは飲食店データの補完・修正アシスタントです。
Step1で抽出された1店舗の情報と、URLから取得したページ内容を照合し、情報を補完・修正してください。
必ず指定されたJSONフォーマットでのみ回答してください。

# 作業内容
1. URLページの内容から shop_name・area・category を補完・修正する。
2. 既存の値が正しければそのまま維持する。誤りや粒度がずれている場合のみ修正する。

# shop_name のルール
- 単一店舗として識別できる正式名称にする。
- ページから支店名まで特定できる場合は補う（例: 天よし 浅草本店）。
- 特定できない場合は元の値を維持。

# area のルール（最重要）
- 必ず「最寄り駅名・街名・街区レベル」に統一する。
- 良い例: 浅草、神田、荻窪、蔵前、中目黒、恵比寿
- 悪い例: 東京都、台東区、渋谷区、東京、関東
- ページの住所・地図情報から最も自然な街・駅レベルに補正する。
- 特定できない場合は元の値（または null）を維持。

# category のルール
- 業態・料理ジャンルに統一する（例: 寿司、天ぷら、ラーメン、居酒屋、カフェ、焼肉）。
- メニュー名・食材名は不可。
- ページの業態表記から最も適切なものを選ぶ。

# 入力フォーマット
{
    "step1_result": { "shop_name": "...", "area": "...", "category": "...", "url": "..." },
    "url_content": "（URLページのテキスト内容）"
}

# 出力フォーマット（Step1と同じ構造で返す）
{
    "shop_name": "天よし 浅草本店",
    "area": "浅草",
    "category": "天ぷら",
    "url": "https://..."
}
"""

# ---------------------------------------------------------------------------
# URL fetch helper
# ---------------------------------------------------------------------------
_URL_FETCH_TIMEOUT = 6.0        # seconds per request
_URL_CONTENT_MAX_CHARS = 4000


async def _fetch_url_content(url: str) -> Optional[str]:
    """Fetch URL as plain text, stripping HTML tags. Returns None on failure."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=_URL_FETCH_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            html = response.text

        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:_URL_CONTENT_MAX_CHARS]

    except Exception as e:
        logger.warning("URL fetch failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Step 2 enrichment for a single shop dict
# ---------------------------------------------------------------------------
async def _enrich_shop(shop: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run Step2 enrichment for a single shop that has a URL.
    Returns the enriched shop dict (falls back to original on error).
    """
    url = shop.get("url")
    if not url:
        return shop

    url_content = await _fetch_url_content(url)
    if not url_content:
        logger.info("URL content unavailable for %s, using Step1 result as-is.", url)
        return shop

    try:
        step2_input = json.dumps(
            {"step1_result": shop, "url_content": url_content},
            ensure_ascii=False,
        )
        response = await openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": STEP2_SYSTEM_PROMPT},
                {"role": "user", "content": step2_input},
            ],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        # Preserve url field if Step2 dropped it
        if "url" not in result:
            result["url"] = url
        return result
    except Exception as e:
        logger.error("Step2 OpenAI API error for %s: %s", url, e)
        return shop  # Fall back to Step1 result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def parse_restaurant_info(text: str) -> Optional[List[Dict[str, Any]]]:
    """
    Extract restaurant info from a Discord message in two steps.

    Returns:
        None            — API error (caller should not mark message as processed)
        []              — Not a restaurant message (ignore)
        [dict, ...]     — One or more shops extracted from the message

    Each shop dict contains: shop_name, area, category, url
    """
    if not openai_client:
        logger.error("OPENAI_API_KEY is missing.")
        return None

    # --- Step 1: extract from text ---
    try:
        step1_response = await openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": STEP1_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
        )
        step1_result = json.loads(step1_response.choices[0].message.content)
    except Exception as e:
        logger.error("Step1 OpenAI API error: %s", e)
        return None

    if step1_result.get("ignore", True):
        return []

    shops: List[Dict[str, Any]] = step1_result.get("shops", [])
    if not shops:
        return []

    # --- Step 2: enrich all shops in parallel ---
    enriched = await asyncio.gather(*(_enrich_shop(s) for s in shops))
    return list(enriched)

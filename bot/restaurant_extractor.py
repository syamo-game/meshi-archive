import os
import json
import logging
from openai import AsyncOpenAI
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Initialize OpenAI Key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
else:
    logger.warning("OPENAI_API_KEY is not set.")
    openai_client = None

SYSTEM_PROMPT = """
あなたはDiscordに投稿されたテキストやURL、埋め込み(Embed)情報から、飲食店の情報を抽出するアシスタントです。

以下のルールに従って、必ず指定されたJSONフォーマットでのみ回答してください。

# 判定ルール
- 外食、テイクアウト、デリバリー、お取り寄せ、スーパーやデパ地下の弁当・惣菜など、みんなにお勧めしたい魅力的な食べ物やお店の情報であればすべて「対象」としてください。
- Youtubeのグルメ動画や、食べログ・X(Twitter)などのリンクが含まれている場合も、本文やタイトルから情報を読み取って対象とします。
- 単なる会話の相槌や、食べ物に全く関係ない話題のみ対象外（ignore: true）とします。

# 抽出ルール
対象となる場合、以下の項目を抽出してJSONで返してください。
- `ignore`: false
- `shop_name`: 店名やブランド名（文字列。不明な場合は null。※重要: 文中の呼び名ではなく、一般的な「正式名称」を推測・補正して出力してください。例：「吉池丸」等の独自の呼び方や商品名が含まれる場合、正式な店名である「吉池」に補正するなど）
- `area`: 最も具体的なエリア名（駅名、市区町村名。文字列。不明な場合は null。例: 上野御徒町、新宿 など）
- `category`: お店のジャンルや食べ物の種類（例: 寿司、居酒屋、海鮮、イタリアン 等。文字列。不明な場合は null）
- `url`: 情報に含まれるURL（文字列。Youtube、食べログ、Xのポストなど、テキスト内に含まれるURLを抽出してください。含まれない場合は null）

# 出力フォーマット例 (対象の場合)
{
    "ignore": false,
    "shop_name": "吉池",
    "area": "御徒町",
    "category": "海鮮",
    "url": "https://..."
}

# 出力フォーマット例 (対象外の場合)
{
    "ignore": true
}
"""

async def parse_restaurant_info(text: str) -> Optional[Dict[str, Any]]:
    """
    Parses Discord text using OpenAI (gpt-4o-mini).
    Returns a dict containing the parsed output or None if it fails.
    """
    if not openai_client:
        logger.error("OPENAI_API_KEY is missing.")
        return None

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ],
            response_format={ "type": "json_object" }
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Error parsing text with OpenAI API: {e}")
        return None

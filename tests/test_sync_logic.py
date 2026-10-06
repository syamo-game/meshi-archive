import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from bot.sync_logic import build_message_envelope, fill_missing_shop_fields
from db.models import Shop
from web.area_groups import AREA_TO_GROUP


class SyncLogicTest(unittest.TestCase):
    def test_build_message_envelope_marks_only_embed_images_as_previews(self) -> None:
        message = SimpleNamespace(
            id=12345678901234567,
            channel=SimpleNamespace(id=22345678901234567),
            content="https://x.com/food/status/1924813456236278183",
            created_at=datetime.now(timezone.utc),
            embeds=[
                SimpleNamespace(
                    url="https://x.com/food/status/1924813456236278183",
                    title="店の投稿",
                    description=None,
                    fields=[],
                    image=SimpleNamespace(
                        url="https://media.discordapp.net/external/preview.jpg"
                    ),
                    thumbnail=SimpleNamespace(
                        url="https://images-ext-1.discordapp.net/external/thumb.jpg"
                    ),
                )
            ],
            attachments=[
                SimpleNamespace(
                    content_type="image/jpeg",
                    filename="receipt.jpg",
                    url="https://cdn.discordapp.com/attachments/1/2/receipt.jpg",
                )
            ],
        )

        envelope = build_message_envelope(message)
        previews = [asset for asset in envelope.assets if asset.is_embed_preview]
        attachment = next(
            asset for asset in envelope.assets if asset.title == "receipt.jpg"
        )

        self.assertEqual(len(previews), 2)
        self.assertTrue(all(asset.kind == "image" for asset in previews))
        self.assertFalse(attachment.is_embed_preview)
        self.assertNotIn("is_embed_preview", previews[0].model_dump())

    def test_attachment_overrides_duplicate_embed_preview_origin(self) -> None:
        shared_url = "https://cdn.discordapp.com/attachments/1/2/receipt.jpg"
        message = SimpleNamespace(
            id=12345678901234567,
            channel=SimpleNamespace(id=22345678901234567),
            content="https://x.com/food/status/1924813456236278183",
            created_at=datetime.now(timezone.utc),
            embeds=[
                SimpleNamespace(
                    url="https://x.com/food/status/1924813456236278183",
                    title=None,
                    description=None,
                    fields=[],
                    image=SimpleNamespace(url=shared_url),
                    thumbnail=None,
                )
            ],
            attachments=[
                SimpleNamespace(
                    content_type="image/jpeg",
                    filename="receipt.jpg",
                    url=shared_url,
                )
            ],
        )

        envelope = build_message_envelope(message)
        shared_asset = next(asset for asset in envelope.assets if asset.url == shared_url)

        self.assertFalse(shared_asset.is_embed_preview)
        self.assertEqual(shared_asset.title, "receipt.jpg")

    def test_embed_image_without_parent_url_is_not_a_reusable_preview(self) -> None:
        image_url = "https://media.discordapp.net/attachments/1/2/image.jpg"
        message = SimpleNamespace(
            id=12345678901234567,
            channel=SimpleNamespace(id=22345678901234567),
            content="https://x.com/food/status/1924813456236278183",
            created_at=datetime.now(timezone.utc),
            embeds=[
                SimpleNamespace(
                    url=None,
                    title=None,
                    description=None,
                    fields=[],
                    image=SimpleNamespace(url=image_url),
                    thumbnail=None,
                )
            ],
            attachments=[],
        )

        envelope = build_message_envelope(message)
        image_asset = next(asset for asset in envelope.assets if asset.url == image_url)

        self.assertFalse(image_asset.is_embed_preview)

    def test_fill_missing_shop_fields_only_sets_blank_values(self) -> None:
        shop = Shop(message_id="1", shop_name="ダイニングびあんど")

        changed = fill_missing_shop_fields(
            shop,
            {"area": " 南砂町 ", "category": "居酒屋"},
            " https://example.com/shop ",
        )

        self.assertTrue(changed)
        self.assertEqual(shop.area, "南砂町")
        self.assertEqual(shop.category, "居酒屋")
        self.assertEqual(shop.url, "https://example.com/shop")

    def test_fill_missing_shop_fields_does_not_overwrite_existing_values(self) -> None:
        shop = Shop(
            message_id="1",
            shop_name="ダイニングびあんど",
            area="清澄白河",
            category="カフェ・喫茶店",
            url="https://example.com/old",
        )

        changed = fill_missing_shop_fields(
            shop,
            {"area": "南砂町", "category": "居酒屋", "url": "https://example.com/new"},
        )

        self.assertFalse(changed)
        self.assertEqual(shop.area, "清澄白河")
        self.assertEqual(shop.category, "カフェ・喫茶店")
        self.assertEqual(shop.url, "https://example.com/old")

    def test_area_group_map_contains_backfilled_areas(self) -> None:
        self.assertEqual(AREA_TO_GROUP["南砂町"], "東京 / 江東区")
        self.assertEqual(AREA_TO_GROUP["築地"], "東京 / 中央区")
        self.assertEqual(AREA_TO_GROUP["等々力"], "東京 / 世田谷区")
        self.assertEqual(AREA_TO_GROUP["陸前高田"], "岩手 / 陸前高田市")
        self.assertEqual(AREA_TO_GROUP["\u9ad8\u8f2a\u53f0"], "\u6771\u4eac / \u6e2f\u533a")
        self.assertEqual(AREA_TO_GROUP["\u9ad8\u77e5"], "高知 / 高知市")


if __name__ == "__main__":
    unittest.main()

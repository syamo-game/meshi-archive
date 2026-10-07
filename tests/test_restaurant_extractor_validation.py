import asyncio
import os
import socket
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

import bot.restaurant_extractor as extractor_module
from bot.restaurant_extractor import (
    ExtractionError,
    ExtractedMention,
    ExtractedMessage,
    ImageClues,
    SearchCandidateSet,
    _estimate_cost_microusd,
    _validate_fetch_url,
    analyze_restaurant_images,
    discover_restaurant_mentions,
    extract_json_ld_candidates,
    fetch_structured_candidates,
    normalize_extracted_shops,
    search_restaurant_candidates,
    validate_shop_info,
)
from web.routers.home import _validate_optional_http_url


class RestaurantExtractorValidationTest(unittest.TestCase):
    def test_openai_client_disables_sdk_retries(self) -> None:
        client = object()
        with (
            patch.object(extractor_module, "_client", None),
            patch.object(extractor_module, "_OPENAI_API_KEY", "test-key"),
            patch.object(
                extractor_module,
                "AsyncOpenAI",
                return_value=client,
            ) as constructor,
        ):
            self.assertIs(extractor_module._get_client(), client)

        constructor.assert_called_once_with(api_key="test-key", max_retries=0)

    def test_candidate_search_requires_one_web_search(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponses:
            async def parse(self, **kwargs: object) -> object:
                captured.update(kwargs)
                return SimpleNamespace(
                    status="completed",
                    max_tool_calls=1,
                    parallel_tool_calls=False,
                    output_parsed=SearchCandidateSet(
                        candidates=[],
                        unresolved_reason="候補なし",
                    ),
                    usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            id="ws_1",
                            type="web_search_call",
                            status="completed",
                            action=SimpleNamespace(type="search", sources=[]),
                        )
                    ],
                )

        mention = ExtractedMention(
            shop_name="銀座 鮨はな",
            branch_name="本店",
            area="銀座",
            category="寿司・回転寿司",
            needs_review=True,
            confidence_reason="出典URLに店名がある",
        )
        fake_client = SimpleNamespace(responses=FakeResponses())
        with patch("bot.restaurant_extractor._get_client", return_value=fake_client):
            result = asyncio.run(search_restaurant_candidates(mention))

        self.assertEqual(captured["tools"], [{"type": "web_search"}])
        self.assertEqual(captured["include"], ["web_search_call.action.sources"])
        self.assertEqual(captured["tool_choice"], "required")
        self.assertEqual(captured["max_tool_calls"], 1)
        self.assertFalse(cast(bool, captured["parallel_tool_calls"]))
        self.assertEqual(result.metrics.web_search_calls, 1)

    def test_candidate_search_rejects_incomplete_web_search(self) -> None:
        mention = ExtractedMention(
            shop_name="銀座 鮨はな",
            branch_name="本店",
            area="銀座",
            category="寿司・回転寿司",
            needs_review=True,
            confidence_reason="出典URLに店名がある",
        )
        for statuses in (
            (),
            ("failed",),
            ("in_progress",),
            ("searching",),
            ("completed", "in_progress"),
            ("completed", "failed"),
            ("completed", "searching", "searching"),
            ("completed", "completed"),
        ):
            with self.subTest(statuses=statuses):
                class FakeResponses:
                    async def parse(self, **_kwargs: object) -> object:
                        return SimpleNamespace(
                            status="completed",
                            max_tool_calls=1,
                            parallel_tool_calls=False,
                            output_parsed=SearchCandidateSet(
                                candidates=[],
                                unresolved_reason="候補なし",
                            ),
                            usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                            output=[
                                SimpleNamespace(
                                    id=f"ws_{index}",
                                    type="web_search_call",
                                    status=status,
                                    action=SimpleNamespace(
                                        type="search",
                                        sources=[],
                                    ),
                                )
                                for index, status in enumerate(statuses)
                            ],
                        )

                fake_client = SimpleNamespace(responses=FakeResponses())
                with patch(
                    "bot.restaurant_extractor._get_client",
                    return_value=fake_client,
                ):
                    with self.assertRaisesRegex(
                        ExtractionError,
                        "Web search did not complete exactly once",
                    ):
                        asyncio.run(search_restaurant_candidates(mention))

    def test_candidate_search_accepts_one_ignored_follow_up_search(self) -> None:
        mention = ExtractedMention(
            shop_name="銀座 鮨はな",
            branch_name="本店",
            area="銀座",
            category="寿司・回転寿司",
            needs_review=True,
            confidence_reason="出典URLに店名がある",
        )

        class FakeResponses:
            async def parse(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    status="completed",
                    max_tool_calls=1,
                    parallel_tool_calls=False,
                    output_parsed=SearchCandidateSet(
                        candidates=[],
                        unresolved_reason="候補なし",
                    ),
                    usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            id="ws_completed",
                            type="web_search_call",
                            status="completed",
                            action=SimpleNamespace(
                                type="search",
                                sources=[
                                    SimpleNamespace(
                                        url="https://example.com/ginza-hana"
                                    )
                                ]
                            ),
                        ),
                        SimpleNamespace(
                            id="ws_ignored",
                            type="web_search_call",
                            status="searching",
                            action=SimpleNamespace(type="open_page"),
                        ),
                    ],
                )

        fake_client = SimpleNamespace(responses=FakeResponses())
        with (
            patch("bot.restaurant_extractor._get_client", return_value=fake_client),
            patch(
                "bot.restaurant_extractor._estimate_cost_microusd",
                return_value=2,
            ) as estimate_cost,
        ):
            result = asyncio.run(search_restaurant_candidates(mention))

        self.assertEqual(result.metrics.web_search_calls, 1)
        self.assertEqual(result.metrics.estimated_cost_microusd, 2)
        estimate_cost.assert_called_once_with(extractor_module.RESOLUTION_MODEL, 8, 3, 2)
        self.assertEqual(result.source_urls, ("https://example.com/ginza-hana",))

    def test_candidate_search_rejects_unfinished_response(self) -> None:
        mention = ExtractedMention(
            shop_name="銀座 鮨はな",
            branch_name="本店",
            area="銀座",
            category="寿司・回転寿司",
            needs_review=True,
            confidence_reason="出典URLに店名がある",
        )

        class FakeResponses:
            async def parse(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    status="incomplete",
                    max_tool_calls=1,
                    parallel_tool_calls=False,
                    output_parsed=SearchCandidateSet(
                        candidates=[],
                        unresolved_reason="候補なし",
                    ),
                    usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            id="ws_1",
                            type="web_search_call",
                            status="completed",
                            action=SimpleNamespace(type="search", sources=[]),
                        )
                    ],
                )

        fake_client = SimpleNamespace(responses=FakeResponses())
        with patch("bot.restaurant_extractor._get_client", return_value=fake_client):
            with self.assertRaisesRegex(
                ExtractionError,
                "Web search response did not complete",
            ):
                asyncio.run(search_restaurant_candidates(mention))

    def test_candidate_search_accepts_completed_page_actions(self) -> None:
        mention = ExtractedMention(
            shop_name="銀座 鮨はな",
            branch_name="本店",
            area="銀座",
            category="寿司・回転寿司",
            needs_review=True,
            confidence_reason="出典URLに店名がある",
        )
        for action_type in ("open_page", "find_in_page"):
            with self.subTest(action_type=action_type):
                class FakeResponses:
                    async def parse(self, **_kwargs: object) -> object:
                        return SimpleNamespace(
                            status="completed",
                            max_tool_calls=1,
                            parallel_tool_calls=False,
                            output_parsed=SearchCandidateSet(
                                candidates=[],
                                unresolved_reason="候補なし",
                            ),
                            usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                            output=[
                                SimpleNamespace(
                                    id="ws_1",
                                    type="web_search_call",
                                    status="completed",
                                    action=SimpleNamespace(
                                        type=action_type,
                                        url="https://example.com/ginza-hana",
                                    ),
                                )
                            ],
                        )

                fake_client = SimpleNamespace(responses=FakeResponses())
                with patch(
                    "bot.restaurant_extractor._get_client",
                    return_value=fake_client,
                ):
                    result = asyncio.run(search_restaurant_candidates(mention))

                self.assertEqual(result.metrics.web_search_calls, 1)
                self.assertEqual(
                    result.source_urls,
                    ("https://example.com/ginza-hana",),
                )

    def test_web_search_rejects_sources_from_unfinished_call(self) -> None:
        response = SimpleNamespace(
            status="completed",
            max_tool_calls=1,
            parallel_tool_calls=False,
            output=[
                SimpleNamespace(
                    id="ws_completed",
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(type="search", sources=[]),
                ),
                SimpleNamespace(
                    id="ws_unfinished",
                    type="web_search_call",
                    status="searching",
                    action=SimpleNamespace(
                        type="search",
                        sources=[SimpleNamespace(url="https://example.com/pending")],
                    ),
                ),
            ],
        )

        with self.assertRaisesRegex(
            ExtractionError,
            "Pending web search output is invalid",
        ):
            extractor_module._completed_web_search_source_urls(
                response,
                "candidate_search",
            )

    def test_web_search_rejects_private_completed_page_url(self) -> None:
        response = SimpleNamespace(
            status="completed",
            max_tool_calls=1,
            parallel_tool_calls=False,
            output=[
                SimpleNamespace(
                    id="ws_1",
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(
                        type="open_page",
                        url="http://127.0.0.1/private",
                    ),
                )
            ],
        )

        with self.assertRaisesRegex(
            ExtractionError,
            "Web search source URL is invalid",
        ):
            extractor_module._completed_web_search_source_urls(
                response,
                "source_discovery",
            )

    def test_source_discovery_uses_luna_strict_output_and_one_required_search(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        class FakeResponses:
            async def parse(self, **kwargs: object) -> object:
                captured.update(kwargs)
                return SimpleNamespace(
                    status="completed",
                    max_tool_calls=1,
                    parallel_tool_calls=False,
                    output_parsed=ExtractedMessage(
                        is_restaurant_message=True,
                        ignore_reason=None,
                        unresolved_reason=None,
                        mentions=[
                            ExtractedMention(
                                shop_name="銀座 鮨はな",
                                branch_name="本店",
                                area="銀座",
                                category="寿司・回転寿司",
                                needs_review=True,
                                confidence_reason="出典URLに店名と所在地がある",
                            )
                        ],
                    ),
                    usage=SimpleNamespace(input_tokens=12, output_tokens=4),
                    output=[
                        SimpleNamespace(
                            id="ws_1",
                            type="web_search_call",
                            status="completed",
                            action=SimpleNamespace(
                                type="search",
                                sources=[
                                    SimpleNamespace(
                                        url="https://example.com/ginza-hana"
                                    )
                                ]
                            ),
                        )
                    ],
                )

        fake_client = SimpleNamespace(responses=FakeResponses())
        with (
            patch("bot.restaurant_extractor._get_client", return_value=fake_client),
            patch(
                "bot.restaurant_extractor.EXTRACTION_MODEL",
                "gpt-5.6-luna",
            ),
        ):
            result = asyncio.run(
                discover_restaurant_mentions(
                    "[Source URL] https://example.com/ginza-hana"
                )
            )

        self.assertEqual(captured["model"], "gpt-5.6-luna")
        self.assertEqual(captured["text_format"], ExtractedMessage)
        self.assertEqual(captured["tools"], [{"type": "web_search"}])
        self.assertEqual(captured["include"], ["web_search_call.action.sources"])
        self.assertEqual(captured["tool_choice"], "required")
        self.assertEqual(captured["max_tool_calls"], 1)
        self.assertFalse(cast(bool, captured["parallel_tool_calls"]))
        self.assertEqual(result.metrics.model, "gpt-5.6-luna")
        self.assertEqual(result.metrics.input_tokens, 12)
        self.assertEqual(result.metrics.output_tokens, 4)
        self.assertEqual(result.metrics.web_search_calls, 1)
        self.assertEqual(result.source_urls, ("https://example.com/ginza-hana",))

    def test_blind_image_analysis_does_not_include_a_candidate_name(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponses:
            async def parse(self, **kwargs: object) -> object:
                captured.update(kwargs)
                return SimpleNamespace(
                    output_parsed=ImageClues(
                        usable=False,
                        image_type="food",
                        visible_shop_names=[],
                        address_clues=[],
                        phone_clues=[],
                        reason="料理写真だけで店舗情報がない",
                    ),
                    usage=None,
                    output=[],
                )

        fake_client = SimpleNamespace(responses=FakeResponses())
        with patch("bot.restaurant_extractor._get_client", return_value=fake_client):
            result = asyncio.run(
                analyze_restaurant_images(
                    None,
                    ["https://cdn.discordapp.com/attachments/1/2/evidence.jpg"],
                )
            )

        model_input = cast(list[dict[str, object]], captured["input"])
        self.assertIsInstance(model_input, list)
        content = cast(list[dict[str, str]], model_input[0]["content"])
        prompt = content[0]["text"]
        self.assertNotIn("候補店名", prompt)
        self.assertFalse(result.clues.usable)

    def test_web_search_tool_fee_is_included_in_estimated_cost(self) -> None:
        previous = os.environ.get("WEB_SEARCH_USD_PER_1K_CALLS")
        os.environ["WEB_SEARCH_USD_PER_1K_CALLS"] = "10"
        try:
            cost = _estimate_cost_microusd(
                "unpriced-test-model",
                input_tokens=0,
                output_tokens=0,
                web_search_calls=2,
            )
        finally:
            if previous is None:
                os.environ.pop("WEB_SEARCH_USD_PER_1K_CALLS", None)
            else:
                os.environ["WEB_SEARCH_USD_PER_1K_CALLS"] = previous
        self.assertEqual(cost, 20_000)

    def test_valid_shop_does_not_need_review(self) -> None:
        result = validate_shop_info(
            {
                "shop_name": "天よし",
                "area": "浅草",
                "category": "天ぷら",
                "url": "https://example.com/shop",
                "extraction_source": "url_content",
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["category"], "天ぷら")
        self.assertEqual(result["url"], "https://example.com/shop")

    def test_missing_area_sets_review_flag(self) -> None:
        result = validate_shop_info(
            {
                "shop_name": "神田まつや",
                "area": None,
                "category": "そば",
                "url": None,
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["needs_review"])
        self.assertIn("area missing", result["confidence_reason"] or "")

    def test_unknown_category_is_preserved_and_reviewed(self) -> None:
        result = validate_shop_info(
            {
                "shop_name": "架空カレー",
                "area": "神田",
                "category": "カレー屋さん",
                "url": None,
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["category"], "カレー屋さん")
        self.assertIn("unknown category", result["confidence_reason"] or "")

    def test_extracted_mention_accepts_new_category_and_normalizes_blank_values(self) -> None:
        mention = ExtractedMention(
            shop_name="PACO（パコ）",
            branch_name=" ",
            area="学芸大学",
            category="メキシコ料理",
            needs_review=False,
            confidence_reason="投稿本文に店名と料理の記載がある",
        )

        self.assertIsNone(mention.branch_name)
        self.assertEqual(mention.category, "メキシコ料理")

    def test_extraction_schema_accepts_per_mention_source_url(self) -> None:
        schema = ExtractedMessage.model_json_schema()
        definitions = next(
            value for key, value in schema.items() if key.endswith("defs")
        )
        properties = definitions["ExtractedMention"]["properties"]

        self.assertIn("source_url", properties)
        self.assertIsNone(properties["source_url"]["default"])

    def test_extracted_mention_rejects_non_http_source_url(self) -> None:
        with self.assertRaises(ValidationError):
            ExtractedMention(
                shop_name="Cafe Alpha",
                branch_name=None,
                area="銀座",
                category="カフェ・喫茶店",
                source_url="file:///etc/passwd",
                needs_review=False,
                confidence_reason="source",
            )

    def test_json_ld_preserves_unknown_cuisine(self) -> None:
        candidates = extract_json_ld_candidates(
            '<script type="application/ld+json">'
            '{"@type":"Restaurant","name":"PACO（パコ）",'
            '"servesCuisine":"メキシコ料理"}'
            "</script>",
            "https://example.com/paco",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "メキシコ料理")

    def test_json_ld_does_not_trust_unrelated_canonical_url(self) -> None:
        page_url = "https://example.com/restaurants/miyabi"
        for candidate_url in ("javascript:alert(1)", "//evil.example/miyabi"):
            with self.subTest(candidate_url=candidate_url):
                candidates = extract_json_ld_candidates(
                    '<script type="application/ld+json">'
                    '{"@type":"Restaurant","name":"割烹みやび",'
                    f'"url":"{candidate_url}"}}'
                    "</script>",
                    page_url,
                )

                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0].canonical_url, page_url)

    def test_invalid_url_is_removed_and_reviewed(self) -> None:
        result = validate_shop_info(
            {
                "shop_name": "天よし",
                "area": "浅草",
                "category": "天ぷら",
                "url": "javascript:alert(1)",
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["needs_review"])
        self.assertIsNone(result["url"])
        self.assertIn("invalid url", result["confidence_reason"] or "")

    def test_fetch_url_validation_blocks_private_hosts(self) -> None:
        allowed, reason = asyncio.run(_validate_fetch_url("http://127.0.0.1/"))

        self.assertFalse(allowed)
        self.assertIn("blocked non-public address", reason)

    def test_fetch_url_validation_blocks_userinfo(self) -> None:
        allowed, reason = asyncio.run(_validate_fetch_url("https://user@example.com/"))

        self.assertFalse(allowed)
        self.assertEqual(reason, "userinfo is not allowed")

    def test_fetch_url_validation_blocks_mixed_public_and_private_dns_answers(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        resolver = AsyncMock(return_value=addresses)

        async def validate() -> tuple[bool, str]:
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", resolver):
                return await _validate_fetch_url("https://example.com/shop")

        allowed, reason = asyncio.run(validate())

        self.assertFalse(allowed)
        self.assertIn("blocked non-public address", reason)
        resolver.assert_awaited_once_with("example.com", 443, type=socket.SOCK_STREAM)

    def test_fetch_url_validation_times_out_dns_lookup(self) -> None:
        async def wait_forever(*_: object, **__: object) -> list[object]:
            await asyncio.Event().wait()
            return []

        resolver = AsyncMock(side_effect=wait_forever)

        async def validate() -> tuple[bool, str]:
            loop = asyncio.get_running_loop()
            with (
                patch.object(loop, "getaddrinfo", resolver),
                patch.object(extractor_module, "URL_FETCH_DNS_TIMEOUT_SECONDS", 0.01),
            ):
                return await _validate_fetch_url("https://example.com/shop")

        allowed, reason = asyncio.run(validate())

        self.assertFalse(allowed)
        self.assertIn("DNS lookup timed out", reason)
        resolver.assert_awaited_once()

    def test_fetch_pins_public_ip_and_preserves_host_and_sni_without_proxy(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html>ok</html>",
            )

        public_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        private_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        resolver = AsyncMock(side_effect=[public_answer, private_answer])
        async_client = httpx.AsyncClient

        async def fetch() -> str:
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", resolver):
                return await extractor_module._fetch_url_content(
                    "https://example.com/shop?q=1"
                )

        with (
            patch.object(
                extractor_module.httpx,
                "AsyncHTTPTransport",
                side_effect=lambda **_: httpx.MockTransport(handler),
            ) as transport_constructor,
            patch.object(
                extractor_module.httpx,
                "AsyncClient",
                wraps=async_client,
            ) as client_constructor,
            patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9"}),
        ):
            html = asyncio.run(fetch())

        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://93.184.216.34/shop?q=1")
        self.assertEqual(requests[0].headers["host"], "example.com")
        self.assertEqual(requests[0].extensions["sni_hostname"], "example.com")
        transport_constructor.assert_called_once_with(retries=0)
        self.assertFalse(client_constructor.call_args.kwargs["trust_env"])

    def test_fetch_resolves_relative_redirect_against_logical_url_per_hop(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": "../menu"})
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html>menu</html>",
            )

        answers = [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        ]
        resolver = AsyncMock(side_effect=answers)
        async_client = httpx.AsyncClient

        async def fetch() -> extractor_module.FetchedHtmlDocument:
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", resolver):
                return await extractor_module.fetch_html_document(
                    "https://example.com/shops/one"
                )

        with (
            patch.object(
                extractor_module.httpx,
                "AsyncHTTPTransport",
                side_effect=lambda **_: httpx.MockTransport(handler),
            ) as transport_constructor,
            patch.object(
                extractor_module.httpx,
                "AsyncClient",
                wraps=async_client,
            ) as client_constructor,
        ):
            document = asyncio.run(fetch())

        self.assertEqual(document.html, "<html>menu</html>")
        self.assertEqual(document.final_url, "https://example.com/menu")
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(transport_constructor.call_count, 2)
        self.assertEqual(client_constructor.call_count, 2)
        self.assertEqual(
            [str(request.url) for request in requests],
            ["https://93.184.216.34/shops/one", "https://8.8.8.8/menu"],
        )
        self.assertEqual([request.headers["host"] for request in requests], ["example.com"] * 2)
        self.assertEqual(
            [request.extensions["sni_hostname"] for request in requests],
            ["example.com"] * 2,
        )

    def test_structured_fetch_wraps_network_error_with_url(self) -> None:
        url = "https://example.com/shop"
        request = httpx.Request("GET", url)
        with patch(
            "bot.restaurant_extractor._fetch_url_content",
            side_effect=httpx.ConnectError("connection failed", request=request),
        ):
            with self.assertRaisesRegex(
                ExtractionError,
                r"URL fetch request failed: url=https://example\.com/shop, "
                r"error=ConnectError: connection failed",
            ):
                asyncio.run(fetch_structured_candidates(url))

    def test_edit_url_validation_allows_http_urls(self) -> None:
        self.assertEqual(
            _validate_optional_http_url(" https://example.com/shop "),
            "https://example.com/shop",
        )

    def test_edit_url_validation_rejects_javascript_urls(self) -> None:
        with self.assertRaises(HTTPException):
            _validate_optional_http_url("javascript:alert(1)")

    def test_shop_without_name_is_dropped(self) -> None:
        with self.assertLogs("bot.restaurant_extractor", level="WARNING"):
            result = normalize_extracted_shops(
                [
                    {
                        "shop_name": None,
                        "area": "浅草",
                        "category": "天ぷら",
                        "url": None,
                    },
                    {
                        "shop_name": "天よし",
                        "area": "浅草",
                        "category": "天ぷら",
                        "url": None,
                    },
                ]
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["shop_name"], "天よし")


if __name__ == "__main__":
    unittest.main()

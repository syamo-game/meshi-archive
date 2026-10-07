from __future__ import annotations

import asyncio
import json
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from bot import restaurant_extractor as extractor


@pytest.mark.parametrize(
    ("operation", "schema_name"),
    [
        ("preflight", "ModelPreflight"),
        ("message", "ExtractedMessage"),
        ("source", "ExtractedMessage"),
        ("candidate", "SearchCandidateSet"),
        ("image", "ImageClues"),
    ],
)
def test_actual_sdk_request_limits_context_and_enforces_output_schema(
    operation: str, schema_name: str,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            json={"error": {"message": "synthetic stop", "type": "invalid_request_error"}},
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            client = AsyncOpenAI(
                api_key="synthetic-test-key",
                base_url="https://model-test.invalid/v1",
                http_client=transport,
                max_retries=0,
            )
            mention = extractor.ExtractedMention(
                shop_name="Synthetic shop", branch_name="Main", area="東京 神田",
                needs_review=True, confidence_reason="Synthetic input",
            )
            with patch.object(extractor, "_get_client", return_value=client):
                with pytest.raises(extractor.ExtractionError, match="synthetic stop"):
                    if operation == "preflight":
                        await extractor.preflight_models()
                    elif operation == "message":
                        await extractor.extract_restaurant_message("Synthetic post body")
                    elif operation == "source":
                        await extractor.discover_restaurant_mentions(
                            "[Source URL] https://example.invalid/source\n"
                            "[Source Title] Synthetic title\n[Discord Message]\nSynthetic post"
                        )
                    elif operation == "candidate":
                        await extractor.search_restaurant_candidates(mention)
                    else:
                        await extractor.analyze_restaurant_images(
                            mention, ["data:image/png;base64,c3ludGhldGlj"]
                        )

    asyncio.run(exercise())

    assert len(requests) == 1
    assert requests[0].url.host == "model-test.invalid"
    payload = cast(dict[str, object], json.loads(requests[0].content))
    assert payload["store"] is False
    assert "previous_response_id" not in payload
    assert "conversation" not in payload
    instructions = cast(str, payload["instructions"])
    assert "schema" not in instructions.lower()
    assert "JSON" in instructions
    assert "出力形式" in instructions or operation == "preflight"
    output_format = cast(dict[str, object], cast(dict[str, object], payload["text"])["format"])
    assert output_format["type"] == "json_schema"
    assert output_format["name"] == schema_name
    assert output_format["strict"] is True
    schema = cast(dict[str, object], output_format["schema"])
    assert schema["additionalProperties"] is False

    supplied = payload["input"]
    if operation == "preflight":
        assert supplied == "preflight"
        assert "tools" not in payload
    elif operation == "message":
        assert supplied == "Synthetic post body"
        assert "tools" not in payload
        assert "URL先の本文、画像、添付ファイルは渡されていません" in instructions
    elif operation == "source":
        assert "[Source URL] https://example.invalid/source" in cast(str, supplied)
        assert "[Discord Message]" in cast(str, supplied)
    elif operation == "candidate":
        assert supplied == "店舗名: Synthetic shop\n支店名: Main\nエリア: 東京 神田"
    else:
        messages = cast(list[dict[str, object]], supplied)
        assert len(messages) == 1
        content = cast(list[dict[str, object]], messages[0]["content"])
        assert content[0] == {"type": "input_text", "text": "候補店名: Synthetic shop\n添付画像: 1枚"}
        assert content[1]["type"] == "input_image"
        assert len(content) == 2
        assert "tools" not in payload

    if operation in {"source", "candidate"}:
        assert payload["tools"] == [{"type": "web_search"}]
        assert payload["tool_choice"] == "required"
        assert payload["max_tool_calls"] == 1


@pytest.mark.parametrize("extra_field", [False, True])
def test_sdk_parser_validates_the_structured_response(extra_field: bool) -> None:
    answer: dict[str, object] = {"ok": True}
    if extra_field:
        answer["unexpected"] = "must fail"

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "resp_synthetic", "object": "response", "created_at": 1,
            "model": "synthetic", "status": "completed",
            "output": [{
                "id": "msg_synthetic", "type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(answer), "annotations": []}],
            }],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            client = AsyncOpenAI(
                api_key="synthetic-test-key", base_url="https://model-test.invalid/v1",
                http_client=transport, max_retries=0,
            )
            if extra_field:
                with pytest.raises(ValidationError):
                    await client.responses.parse(
                        model="synthetic", input="preflight", text_format=extractor.ModelPreflight,
                    )
            else:
                response = await client.responses.parse(
                    model="synthetic", input="preflight", text_format=extractor.ModelPreflight,
                )
                assert response.output_parsed == extractor.ModelPreflight(ok=True)

    asyncio.run(exercise())

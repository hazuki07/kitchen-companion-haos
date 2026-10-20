from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kitchen_companion"))

from app.clients import IntegrationError  # noqa: E402
from app.generation import OpenAIRecipeStepGenerator  # noqa: E402


class RecordingTransport:
    def __init__(self, response: Any):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        return self.response


def response_with(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(payload)}],
            }
        ],
    }


class OpenAIRecipeStepGeneratorTests(unittest.TestCase):
    def test_generates_schema_bound_summary_before_steps(self) -> None:
        transport = RecordingTransport(
            response_with(
                {
                    "summary": "架空食材を切って煮る料理。",
                    "steps": [
                        {
                            "text": "架空食材を切る。",
                            "start_seconds": 15,
                            "evidence": "[15s] 架空食材を切ります",
                            "unresolved": False,
                        }
                    ],
                    "unresolved": [],
                }
            )
        )
        generator = OpenAIRecipeStepGenerator(transport)

        result = generator.generate(
            "[15s] 架空食材を切ります",
            {
                "title": "架空料理",
                "ingredients": [],
                "steps": [],
                "image_url": "https://i.ytimg.com/vi/AbCdEfGhI12/maxresdefault.jpg",
            },
        )

        self.assertEqual(result["summary"], "架空食材を切って煮る料理。")
        self.assertEqual(result["steps"][0]["start_seconds"], 15)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["path"]), ("POST", "/v1/responses"))
        self.assertEqual(call["body"]["model"], "gpt-6-luna")
        self.assertTrue(call["body"]["text"]["format"]["strict"])
        self.assertEqual(call["body"]["text"]["format"]["type"], "json_schema")
        content = call["body"]["input"][1]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1], {
            "type": "input_image",
            "image_url": "https://i.ytimg.com/vi/AbCdEfGhI12/maxresdefault.jpg",
            "detail": "low",
        })

    def test_includes_fixed_review_feedback_in_next_generation(self) -> None:
        transport = RecordingTransport(
            response_with(
                {
                    "summary": "架空の要約。",
                    "steps": [],
                    "unresolved": ["加熱時間"],
                }
            )
        )
        generator = OpenAIRecipeStepGenerator(transport)

        generator.generate(
            "架空の出典",
            {"title": "架空料理", "ingredients": [], "steps": []},
            ["工程を出典だけから作り直してください"],
        )

        content = transport.calls[0]["body"]["input"][1]["content"]
        state = json.loads(content[0]["text"])
        self.assertEqual(
            state["review_feedback"],
            ["工程を出典だけから作り直してください"],
        )

    def test_does_not_send_non_youtube_or_private_image_url(self) -> None:
        transport = RecordingTransport(
            response_with({"summary": "架空の要約。", "steps": [], "unresolved": []})
        )
        generator = OpenAIRecipeStepGenerator(transport)

        generator.generate(
            "架空の出典",
            {
                "title": "架空料理",
                "ingredients": [],
                "steps": [],
                "image_url": "https://192.168.50.175/private-thumbnail.jpg",
            },
        )

        content = transport.calls[0]["body"]["input"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text"])

    def test_rejects_refusal_or_malformed_step(self) -> None:
        refusal = RecordingTransport(
            {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "refusal", "refusal": "fictional refusal"}
            ]}]}
        )
        with self.assertRaises(IntegrationError):
            OpenAIRecipeStepGenerator(refusal).generate("架空の出典", {"title": "架空料理"})

        malformed = RecordingTransport(
            response_with({"summary": "架空の要約", "steps": [{}], "unresolved": []})
        )
        with self.assertRaises(IntegrationError):
            OpenAIRecipeStepGenerator(malformed).generate("架空の出典", {"title": "架空料理"})


if __name__ == "__main__":
    unittest.main()

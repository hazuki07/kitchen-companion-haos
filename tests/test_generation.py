from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kitchen_companion"))

from app.clients import IntegrationError  # noqa: E402
from app.generation import GeminiVideoAnalyzer  # noqa: E402


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


class GeminiVideoAnalyzerTests(unittest.TestCase):
    def test_analyzes_public_youtube_video_with_structured_output(self) -> None:
        response = response_with(
            {
                "summary": "架空食材を煮る料理。",
                "ingredients": [
                    {
                        "text": "架空食材 100g",
                        "evidence": "架空食材 100g",
                        "source": "description",
                        "start_seconds": None,
                        "unresolved": False,
                    }
                ],
                "steps": [
                    {
                        "text": "架空食材を煮る。",
                        "start_seconds": 15,
                        "evidence": "架空食材を鍋で煮ます",
                        "unresolved": False,
                    }
                ],
                "unresolved": [],
            }
        )
        transport = RecordingTransport(response)
        analyzer = GeminiVideoAnalyzer(transport)

        result = analyzer.generate(
            "概要欄:\n架空食材 100g",
            {
                "title": "架空料理",
                "video_url": "https://www.youtube.com/watch?v=AbCdEfGhI12",
                "ingredients": [],
                "steps": [],
            },
        )

        self.assertEqual(result["ingredients"][0]["text"], "架空食材 100g")
        self.assertEqual(result["steps"][0]["start_seconds"], 15)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["path"]), ("POST", "/interactions"))
        self.assertFalse(call["body"]["store"])
        self.assertEqual(call["body"]["input"][0], {
            "type": "video",
            "uri": "https://www.youtube.com/watch?v=AbCdEfGhI12",
        })
        self.assertEqual(call["body"]["response_format"]["mime_type"], "application/json")
        self.assertTrue(call["body"]["response_format"]["schema"]["required"])
        step_schema = call["body"]["response_format"]["schema"]["properties"]["steps"]
        self.assertEqual(
            step_schema["items"]["properties"]["start_seconds"]["type"],
            ["integer", "null"],
        )

    def test_rejects_non_video_youtube_urls_before_api_call(self) -> None:
        transport = RecordingTransport(response_with({}))
        analyzer = GeminiVideoAnalyzer(transport)

        with self.assertRaises(IntegrationError):
            analyzer.generate(
                "",
                {
                    "video_url": "https://www.youtube.com/redirect?url=https://127.0.0.1/",
                    "ingredients": [],
                    "steps": [],
                },
            )

        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()

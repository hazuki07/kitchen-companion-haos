from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kitchen_companion"))

from app.clients import IntegrationError, UrllibJsonTransport  # noqa: E402
from app.jev import JevRecipeImportReviewer  # noqa: E402
from app.server import with_jev_import_review  # noqa: E402


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


def successful_response(**overrides: float) -> dict[str, Any]:
    probabilities = {
        "source_ingredients_complete": 0.96,
        "ingredients_grounded": 0.97,
        "source_steps_complete": 0.94,
        "steps_grounded": 0.95,
        "needs_human_review": 0.02,
        **overrides,
    }
    return {
        "model": "jev-fictional",
        "answers": {
            "content_kind": {
                "type": "choice",
                "choice": "recipe",
                "probabilities": {"recipe": 0.98, "unclear": 0.02},
            },
            **{
                key: {"type": "noul", "noul": value}
                for key, value in probabilities.items()
            },
        },
    }


class JevRecipeImportReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = {
            "title": "架空の煮込み",
            "servings": 2,
            "ingredients": [{"name": "架空食材", "original_text": "架空食材 100g"}],
            "steps": ["架空食材を煮る。"],
            "import_warnings": [],
        }

    def test_reviews_recipe_with_typed_questions(self) -> None:
        transport = RecordingTransport(successful_response())
        reviewer = JevRecipeImportReviewer(transport)

        result = reviewer.review("材料と完成までの作り方", self.recipe)

        self.assertTrue(result["approved"])
        self.assertEqual(result["model"], "jev-fictional")
        self.assertEqual(transport.calls[0]["path"], "/v1/systemone")
        self.assertEqual(transport.calls[0]["body"]["model"], "jev-latest")
        self.assertIn("steps_grounded", transport.calls[0]["body"]["questions"])

    def test_api_key_is_hidden_from_transport_repr(self) -> None:
        transport = UrllibJsonTransport(
            "https://api.typesafe.ai", "fictional-jev-secret", max_response_bytes=500_000
        )
        self.assertNotIn("fictional-jev-secret", repr(transport))

    def test_low_step_coverage_requires_review(self) -> None:
        transport = RecordingTransport(successful_response(source_steps_complete=0.2))
        reviewer = JevRecipeImportReviewer(transport)

        result = with_jev_import_review(self.recipe, "途中までの作り方", reviewer)

        self.assertFalse(result["jev_review"]["approved"])
        self.assertTrue(any("料理完成まで" in item for item in result["import_warnings"]))

    def test_unavailable_review_falls_back_to_manual_confirmation(self) -> None:
        class FailingReviewer:
            def review(self, source_text: str, recipe: dict[str, Any]) -> dict[str, Any]:
                raise IntegrationError("fictional outage")

        result = with_jev_import_review(self.recipe, "架空の出典", FailingReviewer())

        self.assertEqual(result["jev_review"]["status"], "unavailable")
        self.assertTrue(any("手動で確認" in item for item in result["import_warnings"]))

    def test_rejects_malformed_response(self) -> None:
        reviewer = JevRecipeImportReviewer(RecordingTransport({"answers": []}))
        with self.assertRaises(IntegrationError):
            reviewer.review("架空の出典", self.recipe)


if __name__ == "__main__":
    unittest.main()

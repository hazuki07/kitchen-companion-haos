from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kitchen_companion"))

from app.catalog import CombinedRecipeCatalog, MockMealieCatalog  # noqa: E402
from app.clients import (  # noqa: E402
    ExternalIntegrationConfig,
    HomeAssistantCookingClient,
    IntegrationError,
    MealieImageClient,
    MealieReadClient,
)
from app.integrations import RuntimeConfig, StageCookingController  # noqa: E402
from app.server import (  # noqa: E402
    JsonLdParser,
    RecipeStore,
    assert_public_host,
    find_recipe_node,
    flatten_steps,
    parse_ingredient,
    validate_recipe,
)


class RecordingTransport:
    def __init__(self, responses: list[Any]):
        self.responses = responses
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
        return self.responses.pop(0)


class BinaryRecordingTransport:
    def __init__(self, content_type: str, payload: bytes):
        self.content_type = content_type
        self.payload = payload
        self.calls: list[tuple[str, int]] = []

    def request_bytes(self, path: str, *, max_bytes: int) -> tuple[str, bytes]:
        self.calls.append((path, max_bytes))
        return self.content_type, self.payload


class RecipeTests(unittest.TestCase):
    def test_store_round_trip_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            saved = store.save(
                {
                    "title": "架空の豆スープ",
                    "servings": 4,
                    "ingredients": [{"amount": 200, "unit": "g", "name": "豆"}],
                    "steps": ["鍋で煮る。"],
                    "tags": ["架空", "スープ"],
                }
            )
            self.assertEqual(store.get(saved["id"])["title"], "架空の豆スープ")
            self.assertEqual([item["id"] for item in store.list("豆")], [saved["id"]])

    def test_validation_rejects_bad_servings(self) -> None:
        with self.assertRaisesRegex(ValueError, "人数"):
            validate_recipe({"title": "架空", "servings": 0, "ingredients": [], "steps": []})

    def test_fraction_ingredient(self) -> None:
        parsed = parse_ingredient("1/2 個 玉ねぎ")
        self.assertEqual(parsed["amount"], 0.5)
        self.assertEqual(parsed["name"], "玉ねぎ")

    def test_japanese_ingredient_orders(self) -> None:
        self.assertEqual(parse_ingredient("鶏もも肉 300g")["amount"], 300)
        self.assertEqual(parse_ingredient("鶏もも肉 300g")["unit"], "g")
        self.assertEqual(parse_ingredient("しょうゆ 大さじ1")["amount"], 1)
        self.assertEqual(parse_ingredient("しょうゆ 大さじ1")["name"], "しょうゆ")

    def test_private_import_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "ローカルネットワーク"):
            assert_public_host("http://127.0.0.1/recipe")

    def test_json_ld_recipe_parsing(self) -> None:
        payload = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebPage", "name": "example"},
                {
                    "@type": ["Recipe", "Thing"],
                    "name": "架空の煮込み",
                    "recipeInstructions": [
                        {
                            "@type": "HowToSection",
                            "itemListElement": [{"@type": "HowToStep", "text": "煮る。"}],
                        }
                    ],
                },
            ],
        }
        parser = JsonLdParser()
        parser.feed(f'<script type="application/ld+json">{json.dumps(payload)}</script>')
        node = find_recipe_node(json.loads(parser.blocks[0]))
        self.assertEqual(node["name"], "架空の煮込み")
        self.assertEqual(flatten_steps(node["recipeInstructions"]), ["煮る。"])

    def test_stage_runtime_never_claims_real_integrations(self) -> None:
        status = RuntimeConfig(mode="stage").public_status()
        self.assertEqual(status["home_assistant"], "mock")
        self.assertEqual(status["mealie"], "mock_read_only")
        self.assertFalse(status["external_write_enabled"])

    def test_stage_cooking_action_is_simulated(self) -> None:
        recipe = {"id": "fictional", "steps": ["切る。", "架空の鍋で煮る。"]}
        result = StageCookingController().perform(recipe, "next", 1, speak=True)
        self.assertEqual(result["step_number"], 2)
        self.assertEqual(result["text"], "架空の鍋で煮る。")
        self.assertTrue(result["simulated"])

    def test_stage_cooking_action_rejects_unknown_operation(self) -> None:
        with self.assertRaisesRegex(ValueError, "工程操作"):
            StageCookingController().perform({"id": "fictional", "steps": ["切る。"]}, "delete", 1)

    def test_mock_mealie_catalog_is_read_only_and_searchable(self) -> None:
        fixture = ROOT / "kitchen_companion" / "app" / "fixtures" / "mock_mealie.json"
        catalog = MockMealieCatalog(fixture)
        results = catalog.list("きのこ")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_kind"], "mealie_mock")
        self.assertTrue(results[0]["read_only"])

    def test_combined_catalog_includes_drafts(self) -> None:
        fixture = ROOT / "kitchen_companion" / "app" / "fixtures" / "mock_mealie.json"
        with tempfile.TemporaryDirectory() as directory:
            drafts = RecipeStore(Path(directory) / "recipes.sqlite3")
            catalog = CombinedRecipeCatalog(MockMealieCatalog(fixture), drafts)
            self.assertTrue(any(recipe["source_kind"] == "draft" for recipe in catalog.list()))

    def test_mealie_client_uses_read_only_endpoints_and_maps_unknowns(self) -> None:
        transport = RecordingTransport(
            [
                {"items": [{"slug": "fictional-soup"}]},
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "slug": "fictional-soup",
                    "name": "架空スープ",
                    "image": "fictional-image-version",
                    "recipeYield": "",
                    "recipeIngredient": [{"display": "架空の香辛料 適量"}],
                    "recipeInstructions": [{"text": "架空の鍋で煮る。"}],
                    "tags": [{"name": "架空"}],
                },
            ]
        )
        recipes = MealieReadClient(transport).list("スープ")
        self.assertEqual(recipes[0]["title"], "架空スープ")
        self.assertFalse(recipes[0]["servings_confirmed"])
        self.assertEqual(recipes[0]["ingredients"][0]["note"], "架空の香辛料 適量")
        self.assertEqual(
            recipes[0]["image_url"],
            "api/catalog/images/11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET"])
        self.assertEqual(transport.calls[0]["query"]["search"], "スープ")

    def test_home_assistant_client_calls_only_configured_script(self) -> None:
        transport = RecordingTransport([[]])
        client = HomeAssistantCookingClient(transport, "script.fictional_cooking_guide")
        result = client.perform(
            {"id": "fictional-soup", "steps": ["切る。", "煮る。", "盛る。"]},
            "next",
            2,
            speak=True,
        )
        self.assertFalse(result["simulated"])
        self.assertEqual(result["step_number"], 3)
        self.assertEqual(result["speech_output"], "home_assistant")
        self.assertEqual(transport.calls[0]["path"], "/api/services/script/fictional_cooking_guide")
        self.assertEqual(
            transport.calls[0]["body"],
            {
                "recipe_id": "fictional-soup",
                "step_number": 2,
                "operation": "next",
                "speak": True,
            },
        )

    def test_home_assistant_client_rejects_unscoped_entity(self) -> None:
        with self.assertRaisesRegex(ValueError, "entity ID"):
            HomeAssistantCookingClient(RecordingTransport([]), "light.kitchen")

    def test_external_config_hides_fictional_token_from_repr(self) -> None:
        config_json = {
            "mealie": {"url": "https://mealie.example.invalid", "token": "fictional-secret"},
            "home_assistant": {"script_entity_id": "script.fictional_cooking_guide"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integrations.json"
            path.write_text(json.dumps(config_json), encoding="utf-8")
            with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "fictional-supervisor"}):
                config = ExternalIntegrationConfig.load(path)
        self.assertNotIn("fictional-secret", repr(config))
        self.assertEqual(config.script_entity_id, "script.fictional_cooking_guide")

    def test_mealie_image_client_allows_only_images(self) -> None:
        transport = BinaryRecordingTransport("image/webp", b"fictional-image")
        content_type, payload = MealieImageClient(transport).get(
            "11111111-1111-1111-1111-111111111111"
        )
        self.assertEqual(content_type, "image/webp")
        self.assertEqual(payload, b"fictional-image")
        self.assertEqual(
            transport.calls[0][0],
            "/api/media/recipes/11111111-1111-1111-1111-111111111111/images/original.webp",
        )

    def test_mealie_image_client_rejects_non_image(self) -> None:
        client = MealieImageClient(BinaryRecordingTransport("text/html", b"not an image"))
        with self.assertRaises(IntegrationError):
            client.get("11111111-1111-1111-1111-111111111111")

    def test_action_request_unknown_result_is_not_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            request_id = "11111111-1111-1111-1111-111111111111"
            self.assertEqual(store.begin_action(request_id, "fingerprint"), ("new", None))
            store.mark_action_unknown(request_id)
            self.assertEqual(
                store.begin_action(request_id, "fingerprint"),
                ("outcome_unknown", None),
            )

    def test_completed_action_request_returns_stored_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            request_id = "22222222-2222-2222-2222-222222222222"
            store.begin_action(request_id, "fingerprint")
            store.finish_action(request_id, {"step_number": 2})
            self.assertEqual(
                store.begin_action(request_id, "fingerprint"),
                ("completed", {"step_number": 2}),
            )


if __name__ == "__main__":
    unittest.main()

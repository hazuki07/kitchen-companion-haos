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
    HomeAssistantTimerClient,
    IntegrationError,
    MealieImageClient,
    MealieReadClient,
    MealieSyncClient,
)
from app.integrations import (  # noqa: E402
    RuntimeConfig,
    StageCookingController,
    StageTimerController,
)
from app.server import (  # noqa: E402
    JsonLdParser,
    RecipeStore,
    assert_public_host,
    create_normalized_draft,
    extract_description_recipe,
    extract_video_url,
    find_recipe_node,
    flatten_steps,
    parse_ingredient,
    parse_video_timestamp,
    validate_recipe,
    video_draft_from_html,
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
                    "portion_kind": "mixed_meat",
                    "meat_g_per_person": 150,
                    "rice_go_per_person": 0.375,
                    "step_video_timestamps": [15],
                    "ingredients": [{"amount": 200, "unit": "g", "name": "豆"}],
                    "steps": ["鍋で煮る。"],
                    "tags": ["架空", "スープ"],
                }
            )
            self.assertEqual(store.get(saved["id"])["title"], "架空の豆スープ")
            self.assertEqual(store.get(saved["id"])["portion_kind"], "mixed_meat")
            self.assertEqual(store.get(saved["id"])["rice_go_per_person"], 0.375)
            self.assertEqual(store.get(saved["id"])["step_video_timestamps"], [15])
            self.assertEqual([item["id"] for item in store.list("豆")], [saved["id"]])

    def test_validation_rejects_bad_servings(self) -> None:
        with self.assertRaisesRegex(ValueError, "人数"):
            validate_recipe({"title": "架空", "servings": 0, "ingredients": [], "steps": []})

    def test_portion_guidance_defaults_are_separate_from_recipe_amounts(self) -> None:
        recipe = validate_recipe(
            {
                "title": "架空の希釈ドリンク",
                "servings": 2,
                "portion_kind": "diluted_drink",
                "rice_go_per_person": 0.375,
                "ingredients": [{"amount": 500, "unit": "ml", "name": "レシピ記載の液体"}],
                "steps": ["架空の容器で混ぜる。"],
            }
        )
        self.assertEqual(recipe["ingredients"][0]["amount"], 500)
        self.assertEqual(recipe["serving_volume_ml"], 300)
        self.assertEqual(recipe["dilution_concentrate_parts"], 3)
        self.assertEqual(recipe["dilution_mixer_parts"], 7)
        self.assertEqual(recipe["rice_go_per_person"], 0.375)

    def test_meat_portion_defaults_depend_on_recipe_kind(self) -> None:
        common = {"title": "架空の肉料理", "servings": 1, "ingredients": [], "steps": []}
        main = validate_recipe({**common, "portion_kind": "meat_main"})
        mixed = validate_recipe({**common, "portion_kind": "mixed_meat"})
        self.assertEqual(main["meat_g_per_person"], 200)
        self.assertEqual(mixed["meat_g_per_person"], 150)

    def test_validation_rejects_unknown_portion_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "分量タイプ"):
            validate_recipe(
                {
                    "title": "架空",
                    "servings": 1,
                    "portion_kind": "unknown",
                    "ingredients": [],
                    "steps": [],
                }
            )

    def test_fraction_ingredient(self) -> None:
        parsed = parse_ingredient("1/2 個 玉ねぎ")
        self.assertEqual(parsed["amount"], 0.5)
        self.assertEqual(parsed["amount_text"], "1/2")
        self.assertEqual(parsed["name"], "玉ねぎ")
        self.assertEqual(parsed["original_text"], "1/2 個 玉ねぎ")

    def test_validation_preserves_ingredient_source_text(self) -> None:
        recipe = validate_recipe(
            {
                "title": "架空の料理",
                "servings": 2,
                "ingredients": [
                    {
                        "amount": 0.5,
                        "amount_text": "1/2",
                        "unit": "個",
                        "name": "架空野菜",
                        "original_text": "架空野菜 1/2個",
                    }
                ],
                "steps": [],
            }
        )
        ingredient = recipe["ingredients"][0]
        self.assertEqual(ingredient["amount"], 0.5)
        self.assertEqual(ingredient["amount_text"], "1/2")
        self.assertEqual(ingredient["original_text"], "架空野菜 1/2個")

    def test_normalized_mealie_draft_is_reused_and_updates_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            mealie_recipe = {
                "title": "架空の正規化レシピ",
                "servings": 2,
                "servings_confirmed": True,
                "ingredients": [
                    {
                        "amount": 4,
                        "amount_max": 5,
                        "amount_text": "4〜5",
                        "unit": "個",
                        "name": "架空食材",
                    }
                ],
                "steps": ["架空の工程。"],
            }
            draft = create_normalized_draft(store, mealie_recipe, "fictional-recipe")
            store.save({**draft, "title": "確認済みの下書き"}, draft["id"])

            reused = create_normalized_draft(store, mealie_recipe, "fictional-recipe")
            sync_state, slug, _ = store.begin_mealie_sync(draft["id"], "fingerprint")

            self.assertEqual(reused["id"], draft["id"])
            self.assertEqual(reused["title"], "確認済みの下書き")
            self.assertEqual(reused["ingredients"][0]["amount_max"], 5)
            self.assertEqual(sync_state, "update")
            self.assertEqual(slug, "fictional-recipe")

            store.finish_mealie_sync(draft["id"], {"slug": "fictional-recipe"})
            refreshed = create_normalized_draft(
                store,
                {**mealie_recipe, "title": "再取り込み後の正規化レシピ"},
                "fictional-recipe",
            )
            self.assertEqual(refreshed["id"], draft["id"])
            self.assertEqual(refreshed["title"], "再取り込み後の正規化レシピ")

    def test_normalized_mealie_draft_requires_confirmed_servings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            with self.assertRaisesRegex(ValueError, "基準人数"):
                create_normalized_draft(
                    store,
                    {
                        "title": "架空の人数未確認レシピ",
                        "servings": 1,
                        "servings_confirmed": False,
                        "ingredients": [],
                        "steps": [],
                    },
                    "fictional-recipe",
                )

    def test_video_timestamp_accepts_clock_formats(self) -> None:
        self.assertEqual(parse_video_timestamp("01:30"), 90)
        self.assertEqual(parse_video_timestamp("1:02:03"), 3723)
        self.assertIsNone(parse_video_timestamp(""))

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

    def test_json_ld_video_url_is_extracted(self) -> None:
        self.assertEqual(
            extract_video_url({"@type": "VideoObject", "contentUrl": "https://video.example.invalid/watch/1"}),
            "https://video.example.invalid/watch/1",
        )

    def test_video_metadata_prefills_draft(self) -> None:
        html = """
        <html><head>
          <meta property="og:title" content="架空の料理動画">
          <meta property="og:description" content="架空の説明">
          <meta property="og:image" content="https://images.example.invalid/thumb.jpg">
        </head></html>
        """
        draft = video_draft_from_html("https://video.example.invalid/watch/1", html)
        self.assertEqual(draft["title"], "架空の料理動画")
        self.assertEqual(draft["description"], "架空の説明")
        self.assertEqual(draft["video_url"], "https://video.example.invalid/watch/1")
        self.assertEqual(draft["image_url"], "https://images.example.invalid/thumb.jpg")
        self.assertEqual(draft["ingredients"], [])
        self.assertEqual(draft["steps"], [])

    def test_video_description_sections_prefill_recipe(self) -> None:
        description = """架空の説明
材料（3人分）
・豆 150g
・塩 適量
作り方
00:15 豆を洗う。
01:30 架空の鍋で煮る。
#架空料理
"""
        servings, ingredients, steps = extract_description_recipe(description)
        self.assertEqual(servings, 3)
        self.assertEqual(ingredients[0]["name"], "豆")
        self.assertEqual(ingredients[0]["amount"], 150)
        self.assertEqual(steps, ["00:15 豆を洗う。", "01:30 架空の鍋で煮る。"])

        draft = video_draft_from_html(
            "https://video.example.invalid/watch/timestamps",
            f'<meta property="og:description" content="{description}">',
        )
        self.assertEqual(draft["steps"], ["豆を洗う。", "架空の鍋で煮る。"])
        self.assertEqual(draft["step_video_timestamps"], [15, 90])

    def test_video_recipe_json_ld_prefills_ingredients_and_steps(self) -> None:
        payload = {
            "@type": "Recipe",
            "name": "架空の動画レシピ",
            "recipeYield": "2人分",
            "recipeIngredient": ["豆 100g"],
            "recipeInstructions": [{"text": "架空の鍋で煮る。"}],
        }
        html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        draft = video_draft_from_html("https://video.example.invalid/watch/2", html)
        self.assertEqual(draft["ingredients"][0]["name"], "豆")
        self.assertEqual(draft["steps"], ["架空の鍋で煮る。"])
        self.assertEqual(draft["tags"], ["動画"])

    def test_stage_runtime_never_claims_real_integrations(self) -> None:
        status = RuntimeConfig(mode="stage").public_status()
        self.assertEqual(status["home_assistant"], "mock")
        self.assertEqual(status["mealie"], "mock_read_only")
        self.assertFalse(status["external_write_enabled"])

    def test_prototype_runtime_enables_only_home_assistant_timer(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "COOKING_ASSISTANT_MODE": "prototype",
                "COOKING_ASSISTANT_TIMER_SCRIPT_ENTITY_ID": "script.fictional_alexa_timer",
            },
            clear=True,
        ):
            runtime = RuntimeConfig.from_environment()
        status = runtime.public_status()
        self.assertEqual(status["home_assistant"], "timer_script_action")
        self.assertEqual(status["mealie"], "mock_read_only")
        self.assertEqual(status["external_write_scope"], "timer_only")

    def test_prototype_runtime_requires_scoped_timer_script(self) -> None:
        with patch.dict(
            "os.environ",
            {"COOKING_ASSISTANT_MODE": "prototype"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "script entity ID"):
                RuntimeConfig.from_environment()

    def test_home_assistant_runtime_reports_real_mealie_connection(self) -> None:
        status = RuntimeConfig(mode="home_assistant").public_status()
        self.assertEqual(status["mealie"], "manual_sync")
        self.assertTrue(status["mealie_sync_enabled"])

    def test_stage_cooking_action_is_simulated(self) -> None:
        recipe = {"id": "fictional", "steps": ["切る。", "架空の鍋で煮る。"]}
        result = StageCookingController().perform(recipe, "next", 1, speak=True)
        self.assertEqual(result["step_number"], 2)
        self.assertEqual(result["text"], "架空の鍋で煮る。")
        self.assertTrue(result["simulated"])

    def test_stage_cooking_action_rejects_unknown_operation(self) -> None:
        with self.assertRaisesRegex(ValueError, "工程操作"):
            StageCookingController().perform({"id": "fictional", "steps": ["切る。"]}, "delete", 1)

    def test_stage_timer_action_is_simulated(self) -> None:
        result = StageTimerController().perform("start", 5)
        self.assertEqual(result["minutes"], 5)
        self.assertEqual(result["alert_output"], "browser")
        self.assertTrue(result["simulated"])

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

    def test_combined_catalog_hides_synced_draft_when_mealie_has_it(self) -> None:
        class Catalog:
            def list(self, query: str = "") -> list[dict[str, Any]]:
                return [{"id": "fictional-soup", "title": "Mealie側"}]

            def get(self, recipe_id: str) -> dict[str, Any] | None:
                return None

        class Drafts(Catalog):
            def list(self, query: str = "") -> list[dict[str, Any]]:
                return [{
                    "id": "11111111-1111-1111-1111-111111111111",
                    "title": "下書き",
                    "mealie_sync_status": "synced",
                    "mealie_slug": "fictional-soup",
                }]

        self.assertEqual(CombinedRecipeCatalog(Catalog(), Drafts()).list(), [
            {"id": "fictional-soup", "title": "Mealie側"}
        ])

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
                    "extras": {
                        "video_url": "https://video.example.invalid/watch/1",
                        "portion_kind": "drink",
                        "serving_volume_ml": 300,
                        "step_video_timestamps": "[12]",
                    },
                },
            ]
        )
        recipes = MealieReadClient(transport).list("スープ")
        self.assertEqual(recipes[0]["title"], "架空スープ")
        self.assertFalse(recipes[0]["servings_confirmed"])
        self.assertEqual(recipes[0]["video_url"], "https://video.example.invalid/watch/1")
        self.assertEqual(recipes[0]["portion_kind"], "drink")
        self.assertEqual(recipes[0]["serving_volume_ml"], 300)
        self.assertEqual(recipes[0]["step_video_timestamps"], [12])
        self.assertEqual(recipes[0]["ingredients"][0]["name"], "架空の香辛料")
        self.assertEqual(recipes[0]["ingredients"][0]["note"], "適量")
        self.assertEqual(
            recipes[0]["ingredients"][0]["original_text"], "架空の香辛料 適量"
        )
        self.assertEqual(
            recipes[0]["image_url"],
            "api/catalog/images/11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET"])
        self.assertEqual(transport.calls[0]["query"]["search"], "スープ")

    def test_mealie_client_normalizes_concatenated_ingredients(self) -> None:
        repeated_tea = "架空紅茶（種類A、または種類B） ...ティースプーン4〜5杯分"
        transport = RecordingTransport(
            [{
                "slug": "fictional-tea",
                "name": "架空チャイ",
                "recipeServings": 2,
                "recipeIngredient": [
                    {
                        "quantity": 0,
                        "food": {"name": repeated_tea},
                        "note": repeated_tea,
                        "display": f"{repeated_tea} {repeated_tea}",
                    },
                    {
                        "quantity": 0,
                        "food": {"name": "架空香辛料...4個 架空の水...400ml"},
                        "note": "架空香辛料...4個 架空の水...400ml",
                    },
                    {
                        "quantity": 0,
                        "food": {"name": "架空の牛乳...400ml 架空の砂糖...適量"},
                    },
                ],
                "recipeInstructions": [],
            }]
        )

        recipe = MealieReadClient(transport).get("fictional-tea")

        self.assertIsNotNone(recipe)
        assert recipe is not None
        self.assertEqual(
            [item["name"] for item in recipe["ingredients"]],
            ["架空紅茶（種類A、または種類B）", "架空香辛料", "架空の水", "架空の牛乳", "架空の砂糖"],
        )
        tea = recipe["ingredients"][0]
        self.assertEqual(tea["amount"], 4)
        self.assertEqual(tea["amount_max"], 5)
        self.assertEqual(tea["amount_text"], "4〜5")
        self.assertEqual(tea["unit"], "ティースプーン")
        self.assertEqual(recipe["ingredients"][2]["amount"], 400)
        self.assertEqual(recipe["ingredients"][2]["unit"], "ml")
        self.assertIsNone(recipe["ingredients"][4]["amount"])
        self.assertEqual(recipe["ingredients"][4]["note"], "適量")

    def test_mealie_sync_client_creates_then_patches_recipe(self) -> None:
        transport = RecordingTransport(["fictional-soup", {"slug": "fictional-soup"}])
        client = MealieSyncClient(transport)
        recipe = {
            "title": "架空スープ",
            "description": "架空の説明",
            "servings": 2,
            "source_url": "https://recipes.example.invalid/fictional",
            "video_url": "https://video.example.invalid/watch/1",
            "portion_kind": "meat_main",
            "meat_g_per_person": 200,
            "rice_go_per_person": 0.375,
            "step_video_timestamps": [20],
            "ingredients": [{
                "amount": 100,
                "amount_text": "100",
                "unit": "g",
                "name": "架空豆",
                "note": "",
                "original_text": "架空豆 100g",
            }],
            "steps": ["架空の鍋で煮る。"],
            "tags": ["架空"],
        }
        slug = client.create(recipe)
        client.update(slug, recipe)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PATCH"])
        self.assertEqual(transport.calls[0]["body"], {"name": "架空スープ"})
        self.assertEqual(transport.calls[1]["body"]["extras"]["video_url"], recipe["video_url"])
        self.assertEqual(transport.calls[1]["body"]["extras"]["portion_kind"], "meat_main")
        self.assertEqual(transport.calls[1]["body"]["extras"]["rice_go_per_person"], 0.375)
        self.assertEqual(
            transport.calls[1]["body"]["extras"]["step_video_timestamps"], "[20]"
        )
        self.assertEqual(transport.calls[1]["body"]["recipeIngredient"][0]["food"], "架空豆")
        self.assertEqual(
            transport.calls[1]["body"]["recipeIngredient"][0]["originalText"],
            "架空豆 100g",
        )
        self.assertEqual(transport.calls[1]["body"]["tags"], [{"name": "架空"}])

    def test_mealie_sync_record_prevents_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            recipe_id = store.list()[0]["id"]
            self.assertEqual(store.begin_mealie_sync(recipe_id, "fingerprint")[0], "create")
            store.set_mealie_slug(recipe_id, "fictional-soup")
            store.mark_mealie_sync_unknown(recipe_id)
            state, slug, _ = store.begin_mealie_sync(recipe_id, "fingerprint")
            self.assertEqual(state, "outcome_unknown")
            self.assertEqual(slug, "fictional-soup")

    def test_deleting_local_recipe_also_removes_its_sync_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            recipe = store.list()[0]
            store.begin_mealie_sync(recipe["id"], "fingerprint")
            store.set_mealie_slug(recipe["id"], "fictional-soup")
            store.finish_mealie_sync(
                recipe["id"],
                {"status": "synced", "mealie_slug": "fictional-soup"},
            )
            self.assertTrue(store.delete(recipe["id"]))
            with store.connect() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM mealie_sync_records WHERE local_recipe_id = ?",
                    (recipe["id"],),
                ).fetchone()[0]
            self.assertEqual(count, 0)

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

    def test_home_assistant_timer_calls_only_configured_script(self) -> None:
        transport = RecordingTransport([[]])
        client = HomeAssistantTimerClient(transport, "script.fictional_kitchen_timer")
        result = client.perform("start", 5)
        self.assertEqual(result["alert_output"], "alexa")
        self.assertEqual(
            transport.calls[0],
            {
                "method": "POST",
                "path": "/api/services/script/fictional_kitchen_timer",
                "query": None,
                "body": {"operation": "start", "minutes": 5},
            },
        )

    def test_home_assistant_timer_rejects_invalid_minutes(self) -> None:
        client = HomeAssistantTimerClient(
            RecordingTransport([]),
            "script.fictional_kitchen_timer",
        )
        with self.assertRaisesRegex(ValueError, "1分から180分"):
            client.perform("start", 0)

    def test_external_config_hides_fictional_token_from_repr(self) -> None:
        config_json = {
            "mealie": {"url": "https://mealie.example.invalid", "token": "fictional-secret"},
            "home_assistant": {
                "script_entity_id": "script.fictional_cooking_guide",
                "timer_script_entity_id": "script.fictional_kitchen_timer",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integrations.json"
            path.write_text(json.dumps(config_json), encoding="utf-8")
            with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "fictional-supervisor"}):
                config = ExternalIntegrationConfig.load(path)
        self.assertNotIn("fictional-secret", repr(config))
        self.assertEqual(config.script_entity_id, "script.fictional_cooking_guide")
        self.assertEqual(config.timer_script_entity_id, "script.fictional_kitchen_timer")

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

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
    HomeAssistantTodoClient,
    IntegrationError,
    MealieImageClient,
    MealieReadClient,
    MealieSyncClient,
)
from app.integrations import (  # noqa: E402
    RuntimeConfig,
    StageCookingController,
    StageTimerController,
    StageTodoController,
)
from app.server import (  # noqa: E402
    MOCK_MEALIE_FIXTURE,
    JsonLdParser,
    RecipeStore,
    assert_public_host,
    build_recipe_list_items,
    caption_recipe_steps,
    compare_recipe_ingredients,
    create_normalized_draft,
    create_youtube_reimport_draft,
    extract_description_recipe,
    extract_video_url,
    fetch_youtube_caption_segments,
    find_recipe_node,
    flatten_steps,
    ingredient_amounts_match,
    parse_ingredient,
    parse_video_timestamp,
    prepare_local_youtube_reimport,
    preview_review_bundle,
    save_review_bundle,
    validate_recipe,
    video_draft_from_html,
    with_generated_step_review,
    youtube_caption_track_url,
    youtube_reimport_candidates,
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
    @staticmethod
    def reviewed_bundle_recipe(video_id: str = "AbCdEfGhI12") -> dict[str, Any]:
        return {
            "title": "架空の動画レシピ",
            "servings": 2,
            "servings_confirmed": True,
            "source_url": f"https://www.youtube.com/watch?v={video_id}",
            "video_url": f"https://youtu.be/{video_id}?t=15",
            "ingredients": [{"amount": 100, "unit": "g", "name": "架空食材"}],
            "ingredients_complete": True,
            "steps": ["架空食材を調理する。"],
            "steps_complete": True,
            "unresolved_fields": ["火加減は未確認"],
        }

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
            self.assertEqual(
                store.get(saved["id"])["cooking_time_estimate"]["minimum_minutes"],
                5,
            )
            self.assertEqual(
                store.get(saved["id"])["cooking_time_estimate"]["maximum_minutes"],
                12,
            )
            self.assertEqual([item["id"] for item in store.list("豆")], [saved["id"]])

    def test_validation_rejects_bad_servings(self) -> None:
        with self.assertRaisesRegex(ValueError, "人数"):
            validate_recipe({"title": "架空", "servings": 0, "ingredients": [], "steps": []})

    def test_recipe_list_items_scale_ingredients_and_number_tasks(self) -> None:
        recipe = {
            "title": "架空の豆料理",
            "ingredients": [
                {"name": "豆", "amount": 100, "unit": "g", "note": ""},
                {"name": "塩", "amount": None, "unit": "", "note": "適量"},
            ],
            "steps": ["豆を洗う。", "豆を煮る。"],
        }
        self.assertEqual(
            build_recipe_list_items(recipe, "shopping", 1.5),
            ["豆 — 150 g", "塩 — 適量"],
        )
        self.assertEqual(
            build_recipe_list_items(recipe, "tasks", 1.5),
            ["架空の豆料理 — 工程1: 豆を洗う。", "架空の豆料理 — 工程2: 豆を煮る。"],
        )

    def test_recipe_list_items_reject_missing_source_items(self) -> None:
        with self.assertRaisesRegex(ValueError, "材料"):
            build_recipe_list_items({"ingredients": []}, "shopping")
        with self.assertRaisesRegex(ValueError, "工程"):
            build_recipe_list_items({"steps": []}, "tasks")

    def test_review_bundle_detects_normalized_youtube_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            store.save(
                {
                    **self.reviewed_bundle_recipe(),
                    "title": "既存の架空レシピ",
                    "video_url": "https://www.youtube.com/embed/AbCdEfGhI12",
                }
            )
            catalog = CombinedRecipeCatalog(MockMealieCatalog(MOCK_MEALIE_FIXTURE), store)

            review = preview_review_bundle(
                {"recipes": [self.reviewed_bundle_recipe()]}, catalog
            )

            self.assertEqual(review["counts"]["candidate"], 0)
            self.assertEqual(review["counts"]["duplicate"], 1)
            self.assertEqual(review["entries"][0]["duplicate_title"], "既存の架空レシピ")

    def test_review_bundle_saves_unconfirmed_servings_for_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            catalog = CombinedRecipeCatalog(MockMealieCatalog(MOCK_MEALIE_FIXTURE), store)
            recipe = {**self.reviewed_bundle_recipe(), "servings_confirmed": False}
            review = preview_review_bundle({"recipes": [recipe]}, catalog)

            self.assertFalse(review["entries"][0]["servings_confirmed"])
            self.assertEqual(review["entries"][0]["unresolved_fields"][0], "基準人数は未確認")
            self.assertIn("火加減は未確認", review["entries"][0]["unresolved_fields"])

            result = save_review_bundle(
                {
                    "recipes": [recipe],
                    "review_token": review["review_token"],
                    "confirm_count": 1,
                },
                catalog,
                store,
            )

            self.assertEqual(result["saved_count"], 1)
            saved = next(item for item in store.list() if item["title"] == recipe["title"])
            self.assertFalse(saved["servings_confirmed"])
            self.assertIn("要確認", saved["tags"])

    def test_review_bundle_marks_unverified_video_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            catalog = CombinedRecipeCatalog(MockMealieCatalog(MOCK_MEALIE_FIXTURE), store)
            recipe = self.reviewed_bundle_recipe("BcDeFgHiJ34")
            recipe.pop("ingredients_complete")
            recipe.pop("steps_complete")
            recipe["unresolved_fields"] = []

            review = preview_review_bundle({"recipes": [recipe]}, catalog)

            self.assertEqual(review["counts"]["unresolved"], 1)
            self.assertEqual(review["entries"][0]["unresolved_fields"], [
                "概要欄の全材料を確認済みか未確認",
                "動画の完成まで工程を確認済みか未確認",
            ])

    def test_reviewed_bundle_save_requires_matching_preview_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            catalog = CombinedRecipeCatalog(MockMealieCatalog(MOCK_MEALIE_FIXTURE), store)
            recipe = self.reviewed_bundle_recipe()
            review = preview_review_bundle({"recipes": [recipe]}, catalog)

            with self.assertRaisesRegex(ValueError, "プレビュー結果"):
                save_review_bundle(
                    {
                        "recipes": [recipe],
                        "review_token": "0" * 64,
                        "confirm_count": 1,
                    },
                    catalog,
                    store,
                )

            with self.assertRaisesRegex(ValueError, "保存件数"):
                save_review_bundle(
                    {
                        "recipes": [recipe],
                        "review_token": review["review_token"],
                        "confirm_count": 2,
                    },
                    catalog,
                    store,
                )

            result = save_review_bundle(
                {
                    "recipes": [recipe],
                    "review_token": review["review_token"],
                    "confirm_count": 1,
                },
                catalog,
                store,
            )
            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(result["saved_titles"], ["架空の動画レシピ"])
            saved = next(item for item in store.list() if item["title"] == "架空の動画レシピ")
            self.assertIn("火加減は未確認", saved["description"])
            self.assertIn("要確認", saved["tags"])

            repeated = preview_review_bundle({"recipes": [recipe]}, catalog)
            self.assertEqual(repeated["counts"]["candidate"], 0)
            self.assertEqual(repeated["counts"]["duplicate"], 1)

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

    def test_youtube_reimport_preview_only_lists_mealie_youtube_recipes(self) -> None:
        class Catalog:
            def list(self, query: str = "") -> list[dict[str, Any]]:
                return [
                    {
                        "id": "youtube-recipe",
                        "title": "ジョージの架空動画料理",
                        "source_kind": "mealie",
                        "video_url": "https://youtu.be/AbCdEfGhI12?t=15",
                    },
                    {
                        "id": "suijiba-recipe",
                        "title": "架空の煮物",
                        "source_kind": "mealie",
                        "source_url": "https://m.youtube.com/watch?v=BcDeFgHiJ34",
                        "tags": ["炊事場"],
                    },
                    {
                        "id": "fabio-recipe",
                        "title": "ファビオの架空パスタ",
                        "source_kind": "mealie",
                        "source_url": "https://www.youtube.com/shorts/CdEfGhIjK56",
                    },
                    {
                        "id": "okazu-recipe",
                        "title": "架空のおかず",
                        "description": "おかずJAPANの動画",
                        "source_kind": "mealie",
                        "video_url": "https://www.youtube-nocookie.com/embed/DeFgHiJkL78",
                    },
                    {
                        "id": "web-recipe",
                        "title": "架空のWeb料理",
                        "source_kind": "mealie",
                        "source_url": "https://recipes.example.invalid/item",
                    },
                    {
                        "id": "local-youtube",
                        "title": "ローカル下書き",
                        "source_kind": "draft",
                        "video_url": "https://www.youtube.com/watch?v=EfGhIjKlM90",
                    },
                ]

            def get(self, recipe_id: str) -> dict[str, Any] | None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            preview = youtube_reimport_candidates(Catalog(), store)

        self.assertEqual(preview["count"], 5)
        self.assertEqual(preview["mealie_count"], 4)
        self.assertEqual(preview["draft_count"], 1)
        self.assertEqual(preview["entries"][0]["id"], "youtube-recipe")
        self.assertEqual(
            [entry["creator"] for entry in preview["entries"]],
            ["ジョージ", "炊事場", "ファビオ", "おかずJAPAN", "動画URL"],
        )
        self.assertEqual(preview["entries"][-1]["draft_status"], "local")

    def test_youtube_reimport_creates_update_draft_and_preserves_portion_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            mealie_recipe = {
                "title": "以前の架空料理",
                "source_kind": "mealie",
                "video_url": "https://www.youtube.com/watch?v=AbCdEfGhI12",
                "portion_kind": "meat_main",
                "meat_g_per_person": 180,
                "tags": ["既存分類"],
                "ingredients": [{"amount": 100, "unit": "g", "name": "以前の食材"}],
                "steps": ["以前の工程。", "以前の仕上げ。"],
            }
            imported = {
                "title": "再取り込みした架空料理",
                "servings": 2,
                "servings_confirmed": True,
                "source_url": "https://www.youtube.com/watch?v=AbCdEfGhI12",
                "video_url": "https://www.youtube.com/watch?v=AbCdEfGhI12",
                "ingredients": [{"amount": 200, "unit": "g", "name": "架空食材"}],
                "steps": ["架空食材を加熱する。"],
                "tags": ["動画"],
                "import_warnings": ["火加減を確認してください"],
                "jev_review": {"approved": False},
            }

            draft = create_youtube_reimport_draft(
                store, mealie_recipe, "youtube-recipe", imported
            )
            sync_state, slug, _ = store.begin_mealie_sync(draft["id"], "fingerprint")

        self.assertEqual(draft["title"], "再取り込みした架空料理")
        self.assertEqual(draft["portion_kind"], "meat_main")
        self.assertEqual(draft["meat_g_per_person"], 180)
        self.assertEqual(draft["tags"], ["既存分類", "動画"])
        self.assertEqual(draft["import_warnings"], ["火加減を確認してください"])
        self.assertEqual(draft["reimport_comparison"]["previous_ingredient_count"], 1)
        self.assertEqual(draft["reimport_comparison"]["ingredients"]["status"], "changed")
        self.assertEqual(draft["reimport_comparison"]["new_step_count"], 1)
        self.assertEqual((sync_state, slug), ("update", "youtube-recipe"))

    def test_youtube_reimport_does_not_overwrite_unreviewed_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            mealie_recipe = {
                "title": "以前の架空料理",
                "source_kind": "mealie",
                "video_url": "https://youtu.be/AbCdEfGhI12",
            }
            first = create_youtube_reimport_draft(
                store,
                mealie_recipe,
                "youtube-recipe",
                {
                    "title": "確認中の架空料理",
                    "servings": 2,
                    "ingredients": [],
                    "steps": [],
                },
            )
            repeated = create_youtube_reimport_draft(
                store,
                mealie_recipe,
                "youtube-recipe",
                {
                    "title": "上書きしてはいけない料理",
                    "servings": 2,
                    "ingredients": [],
                    "steps": [],
                },
            )

        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(repeated["title"], "確認中の架空料理")
        self.assertEqual(repeated["reimport_status"], "existing_draft")

    def test_local_youtube_reimport_is_unsaved_preview_until_user_confirms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            existing = store.save(
                {
                    "title": "確認済みのローカル料理",
                    "servings": 4,
                    "video_url": "https://youtu.be/AbCdEfGhI12",
                    "ingredients": [{"amount": 100, "unit": "g", "name": "以前の食材"}],
                    "steps": ["以前の工程。"],
                    "tags": ["ファビオ"],
                }
            )
            preview = prepare_local_youtube_reimport(
                existing,
                {
                    "title": "再取得したローカル料理",
                    "servings": 2,
                    "servings_confirmed": True,
                    "video_url": "https://youtu.be/AbCdEfGhI12",
                    "ingredients": [{"amount": 200, "unit": "g", "name": "新しい食材"}],
                    "steps": ["新しい工程。", "仕上げる。"],
                    "tags": ["動画"],
                },
            )
            unchanged = store.get(existing["id"])

        self.assertEqual(preview["id"], existing["id"])
        self.assertEqual(preview["title"], "再取得したローカル料理")
        self.assertEqual(preview["tags"], ["ファビオ", "動画"])
        self.assertEqual(preview["reimport_comparison"]["new_step_count"], 2)
        self.assertEqual(preview["reimport_comparison"]["ingredients"]["status"], "changed")
        self.assertIsNotNone(unchanged)
        assert unchanged is not None
        self.assertEqual(unchanged["title"], "確認済みのローカル料理")

    def test_reimport_ingredient_comparison_ignores_order_and_width(self) -> None:
        previous = {
            "ingredients": [
                {"amount": 100, "unit": "ｇ", "name": "架空 食材", "note": ""},
                {"amount": 0.5, "amount_text": "1/2", "unit": "小さじ", "name": "塩"},
            ]
        }
        current = {
            "ingredients": [
                {"amount": 0.5, "amount_text": "0.5", "unit": "小さじ", "name": "塩"},
                {"amount": 100.0, "unit": "g", "name": "架空 食材", "note": ""},
            ]
        }

        comparison = compare_recipe_ingredients(previous, current)

        self.assertEqual(comparison["status"], "unchanged")
        self.assertEqual(comparison["added"], [])
        self.assertEqual(comparison["removed"], [])

    def test_reimport_ingredient_comparison_reports_quantity_change(self) -> None:
        previous = {
            "ingredients": [
                {
                    "amount": 100,
                    "amount_text": "100",
                    "unit": "g",
                    "name": "架空食材",
                    "original_text": "架空食材 100g",
                }
            ]
        }
        current = {
            "ingredients": [
                {
                    "amount": 200,
                    "amount_text": "200",
                    "unit": "g",
                    "name": "架空食材",
                    "original_text": "架空食材 200g",
                }
            ]
        }

        comparison = compare_recipe_ingredients(previous, current)

        self.assertEqual(comparison["status"], "changed")
        self.assertEqual(comparison["added_count"], 1)
        self.assertEqual(comparison["removed_count"], 1)
        self.assertEqual(comparison["added"], ["架空食材 200g"])
        self.assertEqual(comparison["removed"], ["架空食材 100g"])

    def test_reimport_ingredient_comparison_requires_review_when_both_are_empty(self) -> None:
        comparison = compare_recipe_ingredients({"ingredients": []}, {"ingredients": []})

        self.assertEqual(comparison["status"], "unknown")

    def test_mealie_verification_compares_normalized_amounts(self) -> None:
        local = {
            "ingredients": [{
                "amount": 0.5,
                "amount_max": None,
                "amount_text": "1/2",
                "unit": "小さじ",
                "name": "架空香辛料",
                "note": "",
                "original_text": "架空香辛料:小さじ1/2",
            }]
        }
        remote = {
            "ingredients": [{
                "amount": 0.5,
                "amount_max": None,
                "amount_text": "0.5",
                "unit": "小さじ",
                "name": "架空香辛料",
                "note": "",
                "original_text": "0.5 小さじ 架空香辛料",
            }]
        }
        self.assertTrue(ingredient_amounts_match(local, remote))
        remote["ingredients"][0]["amount"] = 1
        self.assertFalse(ingredient_amounts_match(local, remote))

    def test_mealie_verification_allows_retry_without_creating_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RecipeStore(Path(directory) / "recipes.sqlite3")
            draft = create_normalized_draft(
                store,
                {
                    "title": "架空の確認レシピ",
                    "servings": 2,
                    "servings_confirmed": True,
                    "ingredients": [{"amount": 100, "unit": "g", "name": "架空食材"}],
                    "steps": [],
                },
                "fictional-recipe",
            )
            state, slug, _ = store.begin_mealie_sync(draft["id"], "first")
            self.assertEqual((state, slug), ("update", "fictional-recipe"))
            store.mark_mealie_sync_unknown(draft["id"])

            store.resolve_mealie_sync_verification(
                draft["id"], "fictional-recipe", matched=False
            )
            self.assertIsNone(store.mealie_sync_state(draft["id"]))
            retry_state, retry_slug, _ = store.begin_mealie_sync(draft["id"], "second")
            self.assertEqual((retry_state, retry_slug), ("update", "fictional-recipe"))

    def test_video_timestamp_accepts_clock_formats(self) -> None:
        self.assertEqual(parse_video_timestamp("01:30"), 90)
        self.assertEqual(parse_video_timestamp("1:02:03"), 3723)
        self.assertIsNone(parse_video_timestamp(""))

    def test_japanese_ingredient_orders(self) -> None:
        self.assertEqual(parse_ingredient("鶏もも肉 300g")["amount"], 300)
        self.assertEqual(parse_ingredient("鶏もも肉 300g")["unit"], "g")
        self.assertEqual(parse_ingredient("しょうゆ 大さじ1")["amount"], 1)
        self.assertEqual(parse_ingredient("しょうゆ 大さじ1")["name"], "しょうゆ")

    def test_concatenated_range_and_qualitative_ingredients(self) -> None:
        onion = parse_ingredient("玉ねぎ1〜2個")
        self.assertEqual((onion["amount"], onion["amount_max"], onion["unit"]), (1, 2, "個"))
        salt = parse_ingredient("塩：少々")
        self.assertIsNone(salt["amount"])
        self.assertEqual((salt["name"], salt["note"]), ("塩", "少々"))
        garlic = parse_ingredient("にんにく1片（みじん切り）")
        self.assertEqual((garlic["amount"], garlic["unit"]), (1, "片"))
        self.assertEqual(garlic["note"], "みじん切り")

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

    def test_youtube_caption_track_prefers_manual_japanese(self) -> None:
        player_response = {
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": [
                        {
                            "baseUrl": "https://www.youtube.com/api/timedtext?v=fake&lang=en",
                            "languageCode": "en",
                        },
                        {
                            "baseUrl": "https://www.youtube.com/api/timedtext?v=fake&lang=ja&kind=asr",
                            "languageCode": "ja",
                            "kind": "asr",
                        },
                        {
                            "baseUrl": "https://www.youtube.com/api/timedtext?v=fake&lang=ja",
                            "languageCode": "ja",
                        },
                    ]
                }
            }
        }
        html = f"<script>var ytInitialPlayerResponse = {json.dumps(player_response)};</script>"

        track_url = youtube_caption_track_url(html)

        self.assertEqual(
            track_url,
            "https://www.youtube.com/api/timedtext?v=fake&lang=ja",
        )

    def test_caption_recipe_steps_keeps_cooking_actions_and_timestamps(self) -> None:
        steps, timestamps = caption_recipe_steps(
            [
                (3, "今日は架空の料理を作ります。"),
                (12, "玉ねぎを薄く切ります。"),
                (28, "フライパンに油を入れて炒めます。"),
                (44, "チャンネル登録もお願いします。"),
            ]
        )

        self.assertEqual(
            steps,
            ["玉ねぎを薄く切ります。", "フライパンに油を入れて炒めます。"],
        )
        self.assertEqual(timestamps, [12, 28])

    def test_caption_recipe_steps_splits_unpunctuated_speech_by_time(self) -> None:
        steps, timestamps = caption_recipe_steps(
            [
                (10, "玉ねぎを切っていきます"),
                (18, "次にフライパンを温めます"),
                (27, "油を入れて玉ねぎを炒めます"),
            ]
        )

        self.assertEqual(
            steps,
            ["玉ねぎを切っていきます次にフライパンを温めます", "油を入れて玉ねぎを炒めます"],
        )
        self.assertEqual(timestamps, [10, 27])

    def test_fetch_youtube_caption_segments_reads_json3_without_network(self) -> None:
        payload = {
            "events": [
                {"tStartMs": 12500, "segs": [{"utf8": "玉ねぎを"}, {"utf8": "切ります。"}]},
                {"tStartMs": 16000, "segs": [{"utf8": "[音楽]"}]},
            ]
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def geturl(self) -> str:
                return "https://www.youtube.com/api/timedtext?v=fake&fmt=json3"

            def read(self, _size: int) -> bytes:
                return json.dumps(payload).encode()

        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        with (
            patch("app.server.assert_public_host"),
            patch("app.server.build_opener", return_value=opener),
        ):
            segments = fetch_youtube_caption_segments(
                "https://www.youtube.com/api/timedtext?v=fake&lang=ja"
            )

        self.assertEqual(segments, [(12, "玉ねぎを切ります。")])
        self.assertIn("fmt=json3", opener.request.full_url)
        self.assertEqual(opener.timeout, 12)

    def test_video_uses_public_caption_actions_when_description_has_no_steps(self) -> None:
        draft = video_draft_from_html(
            "https://www.youtube.com/watch?v=AbCdEfGhI12",
            '<meta property="og:title" content="字幕付き架空料理">',
            [
                (15, "架空食材を切ります。"),
                (35, "鍋に入れて煮ます。"),
            ],
        )

        self.assertEqual(draft["steps"], ["架空食材を切ります。", "鍋に入れて煮ます。"])
        self.assertEqual(draft["step_video_timestamps"], [15, 35])
        self.assertIn("公開字幕の調理発話", draft["import_warnings"][-1])

    def test_generated_steps_are_accepted_after_jev_approval(self) -> None:
        class Generator:
            def generate(self, source_text, recipe, feedback=None):
                return {
                    "summary": "架空食材を切って煮る料理。",
                    "steps": [{
                        "text": "架空食材を切る。",
                        "start_seconds": 15,
                        "evidence": "[15s] 架空食材を切ります",
                        "unresolved": False,
                    }],
                    "unresolved": [],
                }

        class Reviewer:
            threshold = 0.75

            def review(self, source_text, recipe):
                return {
                    "status": "reviewed",
                    "decision": "recipe",
                    "recipe_probability": 0.98,
                    "source_ingredients_complete": 0.95,
                    "ingredients_grounded": 0.95,
                    "source_steps_complete": 0.96,
                    "steps_grounded": 0.97,
                    "needs_human_review": 0.02,
                    "approved": True,
                }

        result = with_generated_step_review(
            {
                "title": "架空料理",
                "ingredients": [{"name": "架空食材"}],
                "steps": [],
                "import_warnings": [
                    "概要欄または構造化データに作り方がありません。動画音声は自動要約していないため、工程を確認してください"
                ],
            },
            "[15s] 架空食材を切ります",
            Generator(),
            Reviewer(),
        )

        self.assertEqual(result["generation_status"], "approved")
        self.assertEqual(result["steps"], ["架空食材を切る。"])
        self.assertEqual(result["step_video_timestamps"], [15])
        self.assertEqual(result["generation_attempts"], [
            {"attempt": 1, "status": "approved", "step_count": 1}
        ])
        self.assertFalse(any("作り方がありません" in item for item in result["import_warnings"]))

    def test_generated_steps_retry_only_grounding_failure(self) -> None:
        class Generator:
            def __init__(self):
                self.feedback = []

            def generate(self, source_text, recipe, feedback=None):
                self.feedback.append(list(feedback or []))
                return {
                    "summary": "架空料理の要約。",
                    "steps": [{
                        "text": "架空食材を煮る。",
                        "start_seconds": 30,
                        "evidence": "[30s] 架空食材を煮ます",
                        "unresolved": False,
                    }],
                    "unresolved": [],
                }

        class Reviewer:
            threshold = 0.75

            def __init__(self):
                self.calls = 0

            def review(self, source_text, recipe):
                self.calls += 1
                grounded = 0.3 if self.calls == 1 else 0.96
                return {
                    "status": "reviewed",
                    "decision": "recipe",
                    "recipe_probability": 0.98,
                    "source_ingredients_complete": 0.95,
                    "ingredients_grounded": 0.95,
                    "source_steps_complete": 0.96,
                    "steps_grounded": grounded,
                    "needs_human_review": 0.02,
                    "approved": grounded >= 0.75,
                }

        generator = Generator()
        result = with_generated_step_review(
            {"title": "架空料理", "ingredients": [], "steps": [], "import_warnings": []},
            "[30s] 架空食材を煮ます",
            generator,
            Reviewer(),
        )

        self.assertEqual(result["generation_status"], "approved")
        self.assertEqual(len(result["generation_attempts"]), 2)
        self.assertEqual(generator.feedback[0], [])
        self.assertTrue(any("裏付けられない" in item for item in generator.feedback[1]))

    def test_generated_steps_stop_when_source_is_incomplete(self) -> None:
        class Generator:
            def __init__(self):
                self.calls = 0

            def generate(self, source_text, recipe, feedback=None):
                self.calls += 1
                return {
                    "summary": "途中までの架空料理。",
                    "steps": [{
                        "text": "架空食材を切る。",
                        "start_seconds": None,
                        "evidence": "架空食材を切る",
                        "unresolved": False,
                    }],
                    "unresolved": [],
                }

        class Reviewer:
            threshold = 0.75

            def review(self, source_text, recipe):
                return {
                    "status": "reviewed",
                    "decision": "recipe",
                    "recipe_probability": 0.98,
                    "source_ingredients_complete": 0.95,
                    "ingredients_grounded": 0.95,
                    "source_steps_complete": 0.2,
                    "steps_grounded": 0.95,
                    "needs_human_review": 0.02,
                    "approved": False,
                }

        generator = Generator()
        result = with_generated_step_review(
            {"title": "架空料理", "ingredients": [], "steps": [], "import_warnings": []},
            "途中までの出典",
            generator,
            Reviewer(),
        )

        self.assertEqual(result["generation_status"], "source_incomplete")
        self.assertEqual(generator.calls, 1)

    def test_generated_steps_stop_when_jev_does_not_classify_source_as_recipe(self) -> None:
        class Generator:
            def __init__(self):
                self.calls = 0

            def generate(self, source_text, recipe, feedback=None):
                self.calls += 1
                return {
                    "summary": "料理か判断できない動画。",
                    "steps": [{
                        "text": "架空食材を切る。",
                        "start_seconds": None,
                        "evidence": "架空食材を切る",
                        "unresolved": False,
                    }],
                    "unresolved": [],
                }

        class Reviewer:
            threshold = 0.75

            def review(self, source_text, recipe):
                return {
                    "status": "reviewed",
                    "decision": "not_recipe",
                    "recipe_probability": 0.2,
                    "source_ingredients_complete": 0.95,
                    "ingredients_grounded": 0.95,
                    "source_steps_complete": 0.95,
                    "steps_grounded": 0.95,
                    "needs_human_review": 0.02,
                    "approved": False,
                }

        generator = Generator()
        result = with_generated_step_review(
            {"title": "架空動画", "ingredients": [], "steps": [], "import_warnings": []},
            "架空食材を切る",
            generator,
            Reviewer(),
        )

        self.assertEqual(result["generation_status"], "review_required")
        self.assertEqual(generator.calls, 1)
        self.assertTrue(any("再現可能なレシピ" in item for item in result["import_warnings"]))

    def test_generated_steps_stop_after_three_failed_reviews(self) -> None:
        class Generator:
            def __init__(self):
                self.calls = 0

            def generate(self, source_text, recipe, feedback=None):
                self.calls += 1
                return {
                    "summary": "架空料理の要約。",
                    "steps": [{
                        "text": "架空食材を煮る。",
                        "start_seconds": None,
                        "evidence": "架空食材を煮る",
                        "unresolved": False,
                    }],
                    "unresolved": [],
                }

        class Reviewer:
            threshold = 0.75

            def review(self, source_text, recipe):
                return {
                    "status": "reviewed",
                    "decision": "recipe",
                    "recipe_probability": 0.98,
                    "source_ingredients_complete": 0.95,
                    "ingredients_grounded": 0.95,
                    "source_steps_complete": 0.95,
                    "steps_grounded": 0.2,
                    "needs_human_review": 0.02,
                    "approved": False,
                }

        generator = Generator()
        result = with_generated_step_review(
            {"title": "架空料理", "ingredients": [], "steps": [], "import_warnings": []},
            "架空食材を煮る",
            generator,
            Reviewer(),
        )

        self.assertEqual(result["generation_status"], "review_required")
        self.assertEqual(generator.calls, 3)
        self.assertEqual(len(result["generation_attempts"]), 3)
        self.assertTrue(any("3回のJev検品" in item for item in result["import_warnings"]))

    def test_video_prefers_full_youtube_description_and_extended_headings(self) -> None:
        description = """短くない完全な概要欄
▼食材 2人分
玉ねぎ1個
鶏もも肉300g
【調味料】
しょうゆ：大さじ2
塩：少々
■工程
1. 玉ねぎを切る。
2. 鶏肉を焼く。
【ポイント】
ここから先は材料や工程ではありません。
"""
        player_response = {
            "videoDetails": {
                "title": "完全な架空動画",
                "shortDescription": description,
                "thumbnail": {"thumbnails": [{"url": "https://images.example.invalid/full.jpg"}]},
            }
        }
        html = (
            '<meta property="og:title" content="短いタイトル">'
            '<meta property="og:description" content="短い説明">'
            f"<script>var ytInitialPlayerResponse = {json.dumps(player_response)};</script>"
        )
        draft = video_draft_from_html("https://www.youtube.com/watch?v=AbCdEfGhI12", html)
        self.assertEqual(draft["title"], "完全な架空動画")
        self.assertEqual([item["name"] for item in draft["ingredients"]], [
            "玉ねぎ", "鶏もも肉", "しょうゆ", "塩",
        ])
        self.assertEqual(draft["ingredients"][0]["amount_max"], None)
        self.assertEqual(draft["ingredients"][3]["note"], "少々")
        self.assertEqual(draft["steps"], ["玉ねぎを切る。", "鶏肉を焼く。"])
        self.assertTrue(draft["servings_confirmed"])
        self.assertEqual(draft["import_warnings"], [])

    def test_video_marks_missing_recipe_content_for_review(self) -> None:
        draft = video_draft_from_html(
            "https://video.example.invalid/watch/no-recipe",
            '<meta property="og:title" content="架空の料理動画">',
        )
        self.assertFalse(draft["servings_confirmed"])
        self.assertEqual(len(draft["import_warnings"]), 2)
        self.assertIn("動画音声は自動要約していない", draft["import_warnings"][1])

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
        self.assertFalse(status["jev_review_enabled"])

    def test_runtime_reports_optional_jev_review(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "COOKING_ASSISTANT_MODE": "stage",
                "COOKING_ASSISTANT_JEV_ENABLED": "true",
            },
            clear=True,
        ):
            runtime = RuntimeConfig.from_environment()
        self.assertTrue(runtime.public_status()["jev_review_enabled"])

    def test_runtime_reports_optional_openai_generation(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "COOKING_ASSISTANT_MODE": "stage",
                "COOKING_ASSISTANT_JEV_ENABLED": "true",
                "COOKING_ASSISTANT_OPENAI_GENERATION_ENABLED": "true",
                "COOKING_ASSISTANT_OPENAI_MODEL": "gpt-6-luna",
            },
            clear=True,
        ):
            runtime = RuntimeConfig.from_environment()
        status = runtime.public_status()
        self.assertTrue(status["openai_generation_enabled"])
        self.assertEqual(status["openai_model"], "gpt-6-luna")

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

        recipes = CombinedRecipeCatalog(Catalog(), Drafts()).list()
        self.assertEqual(len(recipes), 1)
        self.assertEqual(recipes[0]["id"], "fictional-soup")
        self.assertIsNone(recipes[0]["calorie_estimate"]["minimum_kcal"])
        self.assertIsNone(recipes[0]["cooking_time_estimate"]["minimum_minutes"])

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

    def test_mealie_client_reads_every_recipe_page(self) -> None:
        class PaginatedTransport:
            def __init__(self) -> None:
                self.list_pages: list[int] = []

            def request(
                self,
                method: str,
                path: str,
                *,
                query: dict[str, str | int] | None = None,
                body: dict[str, Any] | None = None,
            ) -> Any:
                if path == "/api/recipes":
                    page = int((query or {}).get("page", 1))
                    self.list_pages.append(page)
                    start = 0 if page == 1 else 50
                    stop = 50 if page == 1 else 54
                    return {
                        "items": [
                            {"slug": f"fictional-video-{index}"}
                            for index in range(start, stop)
                        ],
                        "total": 54,
                        "pages": 2,
                        "next": 2 if page == 1 else None,
                    }
                slug = path.rsplit("/", 1)[-1]
                index = int(slug.rsplit("-", 1)[-1])
                return {
                    "slug": slug,
                    "name": f"架空の動画料理{index}",
                    "orgURL": f"https://www.youtube.com/watch?v=A{index:010d}",
                    "recipeIngredient": [],
                    "recipeInstructions": [],
                }

        transport = PaginatedTransport()
        recipes = MealieReadClient(transport).list()

        self.assertEqual(len(recipes), 54)
        self.assertEqual(transport.list_pages, [1, 2])
        self.assertTrue(recipes[-1]["source_url"].endswith("A0000000053"))

    def test_mealie_client_reads_mobile_youtube_extra_with_spaced_name(self) -> None:
        transport = RecordingTransport(
            [{
                "slug": "fictional-video",
                "name": "架空の動画料理",
                "recipeIngredient": [],
                "recipeInstructions": [],
                "extras": {
                    "YouTube URL": {"value": "https://m.youtube.com/watch?v=AbCdEfGhI12"}
                },
            }]
        )

        recipe = MealieReadClient(transport).get("fictional-video")

        self.assertIsNotNone(recipe)
        assert recipe is not None
        self.assertEqual(
            recipe["video_url"], "https://m.youtube.com/watch?v=AbCdEfGhI12"
        )

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

    def test_mealie_client_normalizes_colon_ingredients(self) -> None:
        raw_ingredients = [
            "架空ひき肉（配合7:3）:250g",
            "架空塩0.8%-1%:2g",
            "架空ナツメグ:小さじ1/2",
            "架空こしょう:多めの少々",
            "架空卵:1個",
            "架空パン粉:大さじ1",
        ]
        transport = RecordingTransport(
            [{
                "slug": "fictional-hamburg",
                "name": "架空ハンバーグ",
                "recipeServings": 2,
                "recipeIngredient": [
                    {
                        "quantity": 0,
                        "food": {"name": text},
                        "note": text,
                        "display": text,
                    }
                    for text in raw_ingredients
                ],
                "recipeInstructions": [],
            }]
        )

        recipe = MealieReadClient(transport).get("fictional-hamburg")

        self.assertIsNotNone(recipe)
        assert recipe is not None
        ingredients = recipe["ingredients"]
        self.assertEqual(
            [item["name"] for item in ingredients],
            [
                "架空ひき肉（配合7:3）",
                "架空塩0.8%-1%",
                "架空ナツメグ",
                "架空こしょう",
                "架空卵",
                "架空パン粉",
            ],
        )
        self.assertEqual((ingredients[0]["amount"], ingredients[0]["unit"]), (250, "g"))
        self.assertEqual((ingredients[1]["amount"], ingredients[1]["unit"]), (2, "g"))
        self.assertEqual(ingredients[2]["amount"], 0.5)
        self.assertEqual(ingredients[2]["amount_text"], "1/2")
        self.assertEqual(ingredients[2]["unit"], "小さじ")
        self.assertIsNone(ingredients[3]["amount"])
        self.assertEqual(ingredients[3]["note"], "多めの少々")
        self.assertEqual((ingredients[4]["amount"], ingredients[4]["unit"]), (1, "個"))
        self.assertEqual((ingredients[5]["amount"], ingredients[5]["unit"]), (1, "大さじ"))

    def test_mealie_client_keeps_text_out_of_amounts(self) -> None:
        transport = RecordingTransport(
            [{
                "slug": "fictional-stew",
                "name": "架空シチュー",
                "recipeServings": 2,
                "recipeIngredient": [
                    {
                        "quantity": 0,
                        "food": {"name": "架空玉ねぎ 1玉"},
                        "note": "架空玉ねぎ 1玉",
                        "display": "架空玉ねぎ 1玉 架空玉ねぎ 1玉",
                    },
                    {
                        "quantity": 0,
                        "food": {"name": "架空にんじん"},
                        "note": "架空にんじん",
                        "display": "架空にんじん 架空にんじん",
                    },
                ],
                "recipeInstructions": [],
            }]
        )

        recipe = MealieReadClient(transport).get("fictional-stew")

        self.assertIsNotNone(recipe)
        assert recipe is not None
        onion, carrot = recipe["ingredients"]
        self.assertEqual(onion["name"], "架空玉ねぎ")
        self.assertEqual((onion["amount"], onion["unit"]), (1, "玉"))
        self.assertEqual(onion["note"], "")
        self.assertEqual(carrot["name"], "架空にんじん")
        self.assertIsNone(carrot["amount"])
        self.assertEqual(carrot["note"], "")

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

    def test_home_assistant_todo_client_adds_each_item_to_configured_list(self) -> None:
        transport = RecordingTransport([[], []])
        client = HomeAssistantTodoClient(
            transport,
            "todo.fictional_shopping",
            "todo.fictional_tasks",
        )
        result = client.add("shopping", ["架空豆 — 200 g", "架空塩 — 適量"])
        self.assertEqual(result["added_count"], 2)
        self.assertEqual(
            transport.calls,
            [
                {
                    "method": "POST",
                    "path": "/api/services/todo/add_item",
                    "query": None,
                    "body": {
                        "entity_id": "todo.fictional_shopping",
                        "item": "架空豆 — 200 g",
                    },
                },
                {
                    "method": "POST",
                    "path": "/api/services/todo/add_item",
                    "query": None,
                    "body": {
                        "entity_id": "todo.fictional_shopping",
                        "item": "架空塩 — 適量",
                    },
                },
            ],
        )

    def test_home_assistant_todo_client_rejects_missing_or_invalid_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "todo entity ID"):
            HomeAssistantTodoClient(RecordingTransport([]), "light.kitchen")
        client = HomeAssistantTodoClient(RecordingTransport([]))
        with self.assertRaisesRegex(ValueError, "設定されていません"):
            client.add("tasks", ["架空工程"])

    def test_stage_todo_controller_never_performs_external_write(self) -> None:
        result = StageTodoController().add("tasks", ["架空工程"])
        self.assertTrue(result["simulated"])
        self.assertEqual(result["added_count"], 1)

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

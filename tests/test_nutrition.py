from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kitchen_companion"))

from app.nutrition import estimate_recipe_calories  # noqa: E402


class NutritionEstimateTests(unittest.TestCase):
    def test_estimates_recipe_total_and_per_serving(self) -> None:
        estimate = estimate_recipe_calories(
            {
                "servings": 2,
                "servings_confirmed": True,
                "ingredients": [
                    {"name": "鶏もも肉", "amount": 250, "unit": "g"},
                    {"name": "オリーブオイル", "amount": 1, "unit": "大さじ"},
                    {"name": "玉ねぎ", "amount": 1, "unit": "個"},
                    {"name": "水", "amount": 100, "unit": "ml"},
                ],
            }
        )

        self.assertEqual(estimate["minimum_kcal"], 660)
        self.assertEqual(estimate["maximum_kcal"], 660)
        self.assertEqual(estimate["per_serving_minimum_kcal"], 330)
        self.assertEqual(estimate["estimated_ingredient_count"], 4)
        self.assertFalse(estimate["has_unestimated_ingredients"])
        self.assertTrue(estimate["used_unit_assumptions"])

    def test_preserves_range_and_marks_unsupported_ingredients(self) -> None:
        estimate = estimate_recipe_calories(
            {
                "servings": 1,
                "servings_confirmed": False,
                "ingredients": [
                    {"name": "砂糖", "amount": 10, "amount_max": 20, "unit": "g"},
                    {"name": "架空の材料", "amount": 3, "unit": "個"},
                    {"name": "塩", "amount": None, "unit": "", "note": "適量"},
                ],
            }
        )

        self.assertEqual(estimate["minimum_kcal"], 39)
        self.assertEqual(estimate["maximum_kcal"], 78)
        self.assertIsNone(estimate["per_serving_minimum_kcal"])
        self.assertEqual(estimate["estimated_ingredient_count"], 1)
        self.assertTrue(estimate["has_unestimated_ingredients"])

    def test_returns_unavailable_when_no_quantity_can_be_estimated(self) -> None:
        estimate = estimate_recipe_calories(
            {"ingredients": [{"name": "こしょう", "amount": None, "unit": "", "note": "少々"}]}
        )

        self.assertIsNone(estimate["minimum_kcal"])
        self.assertIsNone(estimate["maximum_kcal"])
        self.assertTrue(estimate["has_unestimated_ingredients"])

    def test_does_not_treat_water_greens_as_water(self) -> None:
        estimate = estimate_recipe_calories(
            {"ingredients": [{"name": "水菜", "amount": 100, "unit": "g"}]}
        )

        self.assertIsNone(estimate["minimum_kcal"])


if __name__ == "__main__":
    unittest.main()

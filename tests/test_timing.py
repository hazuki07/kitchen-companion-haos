from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kitchen_companion"))

from app.timing import estimate_cooking_time  # noqa: E402


class CookingTimeEstimateTests(unittest.TestCase):
    def test_sums_explicit_ranges_hours_and_seconds(self) -> None:
        estimate = estimate_cooking_time(
            {
                "steps": [
                    "弱火で10〜15分煮る。",
                    "冷蔵庫で1時間休ませる。",
                    "30秒混ぜる。",
                ]
            }
        )
        self.assertEqual(estimate["minimum_minutes"], 71)
        self.assertEqual(estimate["maximum_minutes"], 76)
        self.assertEqual(estimate["explicit_step_count"], 3)
        self.assertEqual(estimate["confidence"], "high")

    def test_does_not_treat_fraction_as_minutes(self) -> None:
        estimate = estimate_cooking_time({"steps": ["トマト缶を3分の1加えて混ぜる。"]})
        self.assertEqual(estimate["explicit_step_count"], 0)
        self.assertEqual(estimate["minimum_minutes"], 3)
        self.assertEqual(estimate["maximum_minutes"], 8)

    def test_uses_action_ranges_only_for_steps_without_time(self) -> None:
        estimate = estimate_cooking_time(
            {"steps": ["玉ねぎを切る。", "色づくまで炒める。", "皿に盛り付ける。"]}
        )
        self.assertEqual(estimate["minimum_minutes"], 10)
        self.assertEqual(estimate["maximum_minutes"], 25)
        self.assertEqual(estimate["estimated_step_count"], 3)
        self.assertEqual(estimate["confidence"], "low")

    def test_returns_unavailable_without_steps(self) -> None:
        estimate = estimate_cooking_time({"steps": []})
        self.assertIsNone(estimate["minimum_minutes"])
        self.assertIsNone(estimate["maximum_minutes"])
        self.assertEqual(estimate["confidence"], "unavailable")


if __name__ == "__main__":
    unittest.main()

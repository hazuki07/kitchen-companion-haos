from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        mode = os.environ.get("COOKING_ASSISTANT_MODE", "stage").strip().lower()
        if mode not in {"stage", "home_assistant"}:
            raise ValueError("COOKING_ASSISTANT_MODEはstageまたはhome_assistantを指定してください")
        return cls(mode=mode)

    def public_status(self) -> dict[str, Any]:
        if self.mode == "stage":
            return {
                "mode": self.mode,
                "label": "Stage（HA・Mealieモック）",
                "home_assistant": "mock",
                "mealie": "mock_read_only",
                "speech": "browser",
                "external_write_enabled": False,
            }
        return {
            "mode": self.mode,
            "label": "Home Assistant（Mealie読取専用）",
            "home_assistant": "script_action",
            "mealie": "read_only",
            "speech": "home_assistant",
            "external_write_enabled": True,
        }


class StageCookingController:
    """実Home Assistantへ接続せずに工程操作の契約を検証するコントローラー。"""

    OPERATIONS = {"start", "show", "next", "previous"}

    def perform(
        self,
        recipe: dict[str, Any],
        operation: str,
        step_number: int,
        speak: bool = False,
    ) -> dict[str, Any]:
        if operation not in self.OPERATIONS:
            raise ValueError("工程操作が正しくありません")
        steps = recipe.get("steps", [])
        if not steps:
            raise ValueError("工程が登録されていません")
        if operation == "start":
            index = 0
        else:
            index = max(0, min(len(steps) - 1, step_number - 1))
            if operation == "next":
                index = min(len(steps) - 1, index + 1)
            elif operation == "previous":
                index = max(0, index - 1)
        return {
            "mode": "stage",
            "simulated": True,
            "recipe_id": recipe["id"],
            "step_number": index + 1,
            "step_count": len(steps),
            "text": steps[index],
            "speak": bool(speak),
            "speech_output": "browser",
        }


class StageTimerController:
    """実Home Assistantへ接続せずタイマー操作の契約を検証する。"""

    ACTIONS = {"start", "stop"}
    MAX_MINUTES = 180

    def perform(self, action: str, minutes: int) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError("タイマー操作が正しくありません")
        if action == "start" and not 1 <= minutes <= self.MAX_MINUTES:
            raise ValueError("タイマーは1分から180分で指定してください")
        if action == "stop":
            minutes = 0
        return {
            "mode": "stage",
            "simulated": True,
            "operation": action,
            "minutes": minutes,
            "alert_output": "browser",
        }

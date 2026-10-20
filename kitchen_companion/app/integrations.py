from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    timer_script_entity_id: str = field(default="", repr=False)
    shopping_list_entity_id: str = field(default="", repr=False)
    task_list_entity_id: str = field(default="", repr=False)
    jev_enabled: bool = False
    openai_generation_enabled: bool = False
    openai_model: str = "gpt-6-luna"

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        mode = os.environ.get("COOKING_ASSISTANT_MODE", "stage").strip().lower()
        if mode not in {"stage", "prototype", "home_assistant"}:
            raise ValueError(
                "COOKING_ASSISTANT_MODEはstage、prototype、home_assistantのいずれかを指定してください"
            )
        timer_script_entity_id = os.environ.get(
            "COOKING_ASSISTANT_TIMER_SCRIPT_ENTITY_ID", ""
        ).strip()
        if mode == "prototype" and not re.fullmatch(
            r"script\.[a-z0-9_]+", timer_script_entity_id
        ):
            raise ValueError("prototypeではAlexaタイマー用script entity IDが必要です")
        shopping_list_entity_id = os.environ.get(
            "COOKING_ASSISTANT_SHOPPING_LIST_ENTITY_ID", ""
        ).strip()
        task_list_entity_id = os.environ.get(
            "COOKING_ASSISTANT_TASK_LIST_ENTITY_ID", ""
        ).strip()
        for entity_id in (shopping_list_entity_id, task_list_entity_id):
            if entity_id and not re.fullmatch(r"todo\.[a-z0-9_]+", entity_id):
                raise ValueError("買い物・タスクリストにはtodo entity IDを指定してください")
        jev_enabled = os.environ.get("COOKING_ASSISTANT_JEV_ENABLED", "").strip().casefold() in {
            "1", "true", "yes", "on",
        }
        openai_generation_enabled = os.environ.get(
            "COOKING_ASSISTANT_OPENAI_GENERATION_ENABLED", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        openai_model = os.environ.get(
            "COOKING_ASSISTANT_OPENAI_MODEL", "gpt-6-luna"
        ).strip()
        if not re.fullmatch(r"gpt-[a-zA-Z0-9._-]+", openai_model):
            raise ValueError("OpenAI model名が正しくありません")
        return cls(
            mode=mode,
            timer_script_entity_id=timer_script_entity_id,
            shopping_list_entity_id=shopping_list_entity_id,
            task_list_entity_id=task_list_entity_id,
            jev_enabled=jev_enabled,
            openai_generation_enabled=openai_generation_enabled,
            openai_model=openai_model,
        )

    def public_status(self) -> dict[str, Any]:
        if self.mode == "stage":
            return {
                "mode": self.mode,
                "label": "Stage（HA・Mealieモック）",
                "home_assistant": "mock",
                "mealie": "mock_read_only",
                "speech": "browser",
                "external_write_enabled": False,
                "shopping_list_enabled": True,
                "task_list_enabled": True,
                "jev_review_enabled": self.jev_enabled,
                "openai_generation_enabled": self.openai_generation_enabled,
                "openai_model": self.openai_model,
            }
        if self.mode == "prototype":
            return {
                "mode": self.mode,
                "label": "試作（HA Alexaタイマー／レシピ・工程モック）",
                "home_assistant": "timer_script_action",
                "mealie": "mock_read_only",
                "speech": "browser",
                "external_write_enabled": True,
                "external_write_scope": "timer_only",
                "shopping_list_enabled": False,
                "task_list_enabled": False,
                "jev_review_enabled": self.jev_enabled,
                "openai_generation_enabled": self.openai_generation_enabled,
                "openai_model": self.openai_model,
            }
        return {
            "mode": self.mode,
            "label": "Home Assistant（Mealie手動同期）",
            "home_assistant": "script_action",
            "mealie": "manual_sync",
            "mealie_sync_enabled": True,
            "speech": "home_assistant",
            "external_write_enabled": True,
            "external_write_scope": "home_assistant_and_mealie_manual",
            "shopping_list_enabled": bool(self.shopping_list_entity_id),
            "task_list_enabled": bool(self.task_list_entity_id),
            "jev_review_enabled": self.jev_enabled,
            "openai_generation_enabled": self.openai_generation_enabled,
            "openai_model": self.openai_model,
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


class StageTodoController:
    """実Home Assistantへ接続せずリスト追加の契約を検証する。"""

    KINDS = {"shopping", "tasks"}

    def add(self, kind: str, items: list[str]) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise ValueError("リスト種別が正しくありません")
        if not items:
            raise ValueError("追加する項目がありません")
        return {
            "mode": "stage",
            "simulated": True,
            "kind": kind,
            "added_count": len(items),
        }

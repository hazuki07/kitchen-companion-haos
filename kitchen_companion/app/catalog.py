from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class RecipeReader(Protocol):
    def list(self, query: str = "") -> list[dict[str, Any]]: ...

    def get(self, recipe_id: str) -> dict[str, Any] | None: ...


class MockMealieCatalog:
    """Stage専用の読み取り専用Mealie互換カタログ。"""

    def __init__(self, fixture_path: Path):
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        recipes = raw.get("recipes", [])
        if not isinstance(recipes, list):
            raise ValueError("Mealieモックfixtureの形式が正しくありません")
        self._recipes = []
        for raw_recipe in recipes:
            recipe = dict(raw_recipe)
            recipe["source_kind"] = "mealie_mock"
            recipe["read_only"] = True
            self._recipes.append(recipe)

    def list(self, query: str = "") -> list[dict[str, Any]]:
        token = query.strip().casefold()
        if not token:
            return [dict(recipe) for recipe in self._recipes]
        return [
            dict(recipe)
            for recipe in self._recipes
            if token
            in " ".join(
                [
                    str(recipe.get("title", "")),
                    str(recipe.get("description", "")),
                    " ".join(recipe.get("tags", [])),
                    " ".join(item.get("name", "") for item in recipe.get("ingredients", [])),
                ]
            ).casefold()
        ]

    def get(self, recipe_id: str) -> dict[str, Any] | None:
        return next((dict(recipe) for recipe in self._recipes if recipe.get("id") == recipe_id), None)


class CombinedRecipeCatalog:
    def __init__(self, primary: RecipeReader, drafts: RecipeReader):
        self.primary = primary
        self.drafts = drafts

    def list(self, query: str = "") -> list[dict[str, Any]]:
        return [*self.primary.list(query), *self.drafts.list(query)]

    def get(self, recipe_id: str) -> dict[str, Any] | None:
        return self.primary.get(recipe_id) or self.drafts.get(recipe_id)

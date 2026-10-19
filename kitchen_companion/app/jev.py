from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .clients import IntegrationError, JsonTransport


class RecipeImportReviewer(Protocol):
    def review(self, source_text: str, recipe: dict[str, Any]) -> dict[str, Any]: ...


def _probability(answer: Any, key: str) -> float | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class JevRecipeImportReviewer:
    transport: JsonTransport
    model: str = "jev-latest"
    threshold: float = 0.75

    def review(self, source_text: str, recipe: dict[str, Any]) -> dict[str, Any]:
        state = {
            "source_text": str(source_text or "")[:30_000],
            "candidate_recipe": {
                "title": str(recipe.get("title") or "")[:160],
                "servings": recipe.get("servings"),
                "ingredients": [
                    str(item.get("original_text") or item.get("name") or "")[:500]
                    for item in recipe.get("ingredients", [])[:200]
                    if isinstance(item, dict)
                ],
                "steps": [str(step)[:2_000] for step in recipe.get("steps", [])[:100]],
            },
        }
        response = self.transport.request(
            "POST",
            "/v1/systemone",
            body={
                "state": state,
                "model": self.model,
                "questions": {
                    "content_kind": {
                        "type": "choice",
                        "instructions": "公開された出典本文は、再現可能な料理レシピを含みますか",
                        "criteria": {
                            "recipe": "材料または調理工程があり、料理を再現する情報を含む",
                            "food_non_recipe": "料理の話題だが再現可能なレシピ情報ではない",
                            "unrelated": "料理レシピとは無関係",
                            "unclear": "本文だけでは判断できない",
                        },
                    },
                    "source_ingredients_complete": {
                        "type": "noul",
                        "instructions": "出典本文には、料理に使う材料が最後まで十分に列挙されている",
                    },
                    "ingredients_grounded": {
                        "type": "noul",
                        "instructions": (
                            "候補レシピの全材料が出典本文で裏付けられ、出典の材料を省略していない"
                        ),
                    },
                    "source_steps_complete": {
                        "type": "noul",
                        "instructions": (
                            "出典本文には、調理開始から料理の完成までの工程が十分に記載されている"
                        ),
                    },
                    "steps_grounded": {
                        "type": "noul",
                        "instructions": (
                            "候補レシピの全工程が出典本文で裏付けられ、"
                            "記載された工程を途中で省略していない"
                        ),
                    },
                    "needs_human_review": {
                        "type": "noul",
                        "instructions": "材料または工程を保存する前に、人が出典を確認する必要がある",
                    },
                },
            },
        )
        if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
            raise IntegrationError("Jevから不正な検品結果を受け取りました")
        answers = response["answers"]
        content = answers.get("content_kind")
        content = content if isinstance(content, dict) else {}
        decision = str(content.get("choice") or "unclear")
        probabilities = content.get("probabilities")
        probabilities = probabilities if isinstance(probabilities, dict) else {}
        recipe_probability = _probability(probabilities, "recipe")
        result = {
            "status": "reviewed",
            "model": str(response.get("model") or self.model)[:80],
            "decision": decision,
            "recipe_probability": recipe_probability,
            "source_ingredients_complete": _probability(
                answers.get("source_ingredients_complete"), "noul"
            ),
            "ingredients_grounded": _probability(answers.get("ingredients_grounded"), "noul"),
            "source_steps_complete": _probability(answers.get("source_steps_complete"), "noul"),
            "steps_grounded": _probability(answers.get("steps_grounded"), "noul"),
            "needs_human_review": _probability(answers.get("needs_human_review"), "noul"),
        }
        required = (
            result["recipe_probability"],
            result["source_ingredients_complete"],
            result["ingredients_grounded"],
            result["source_steps_complete"],
            result["steps_grounded"],
        )
        result["approved"] = (
            decision == "recipe"
            and all(value is not None and value >= self.threshold for value in required)
            and result["needs_human_review"] is not None
            and result["needs_human_review"] < 1 - self.threshold
        )
        return result

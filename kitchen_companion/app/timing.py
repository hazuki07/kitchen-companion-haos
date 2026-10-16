from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

DURATION_PATTERN = re.compile(
    r"(?P<minimum>\d+(?:\.\d+)?)\s*"
    r"(?:(?:〜|～|~|－|-|から)\s*(?P<maximum>\d+(?:\.\d+)?)\s*)?"
    r"(?P<unit>時間|分(?!の)|秒)"
)

HEURISTIC_RANGES = (
    (("煮込", "漬け込", "休ませ", "寝かせ", "発酵", "冷や"), (10, 25)),
    (("炒め", "焼", "揚げ", "茹で", "ゆで", "蒸", "煮る", "加熱"), (5, 12)),
    (("切", "刻", "むく", "おろ", "洗", "混ぜ", "こね", "潰", "つぶ"), (3, 8)),
    (("盛り", "添え", "仕上げ"), (2, 5)),
)
DEFAULT_STEP_RANGE = (3, 8)


def _normalized(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _minutes(value: float, unit: str) -> float:
    if unit == "時間":
        return value * 60
    if unit == "秒":
        return value / 60
    return value


def _explicit_duration(step: str) -> tuple[float, float] | None:
    minimum = 0.0
    maximum = 0.0
    matches = 0
    for match in DURATION_PATTERN.finditer(_normalized(step)):
        low = float(match.group("minimum"))
        high = float(match.group("maximum") or low)
        unit = match.group("unit")
        minimum += _minutes(min(low, high), unit)
        maximum += _minutes(max(low, high), unit)
        matches += 1
    return (minimum, maximum) if matches else None


def _heuristic_duration(step: str) -> tuple[int, int]:
    normalized = _normalized(step)
    for keywords, duration in HEURISTIC_RANGES:
        if any(keyword in normalized for keyword in keywords):
            return duration
    return DEFAULT_STEP_RANGE


def estimate_cooking_time(recipe: dict[str, Any]) -> dict[str, Any]:
    raw_steps = recipe.get("steps")
    steps = (
        [str(step).strip() for step in raw_steps if str(step).strip()]
        if isinstance(raw_steps, list)
        else []
    )
    if not steps:
        return {
            "minimum_minutes": None,
            "maximum_minutes": None,
            "step_count": 0,
            "explicit_step_count": 0,
            "estimated_step_count": 0,
            "confidence": "unavailable",
        }

    minimum = 0.0
    maximum = 0.0
    explicit_count = 0
    for step in steps:
        explicit = _explicit_duration(step)
        if explicit is not None:
            low, high = explicit
            explicit_count += 1
        else:
            low, high = _heuristic_duration(step)
        minimum += low
        maximum += high

    if explicit_count == len(steps):
        confidence = "high"
    elif explicit_count >= max(1, math.ceil(len(steps) / 2)):
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "minimum_minutes": max(1, math.ceil(minimum)),
        "maximum_minutes": max(1, math.ceil(maximum)),
        "step_count": len(steps),
        "explicit_step_count": explicit_count,
        "estimated_step_count": len(steps) - explicit_count,
        "confidence": confidence,
    }


def with_cooking_time_estimate(recipe: dict[str, Any]) -> dict[str, Any]:
    result = dict(recipe)
    result["cooking_time_estimate"] = estimate_cooking_time(result)
    return result

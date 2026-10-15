from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnergyProfile:
    keywords: tuple[str, ...]
    kcal_per_100g: float | None = None
    kcal_per_100ml: float | None = None
    kcal_per_unit: tuple[tuple[str, float], ...] = ()


# 一般的な食品成分値を丸めた概算値。固有の商品や調理後の吸油量は表現しない。
# より具体的な名前を先に並べ、最初に一致したプロファイルを使用する。
ENERGY_PROFILES = (
    EnergyProfile(("オリーブオイル", "サラダ油", "ごま油", "こめ油", "米油"), 884, 820),
    EnergyProfile(("バター",), 720),
    EnergyProfile(("マヨネーズ",), 670, 630),
    EnergyProfile(("グラニュー糖", "砂糖", "はちみつ", "蜂蜜"), 390, 420),
    EnergyProfile(("パン粉",), 370),
    EnergyProfile(("薄力粉", "強力粉", "小麦粉", "片栗粉"), 350),
    EnergyProfile(("乾麺", "パスタ", "スパゲッティ", "うどん（乾", "そば（乾"), 350),
    EnergyProfile(("ごはん", "御飯", "炊いた米"), 156),
    EnergyProfile(("白米", "玄米", "もち米", "米"), 345),
    EnergyProfile(("牛ひき肉", "合いびき肉", "合挽き肉"), 250),
    EnergyProfile(("豚ひき肉",), 220),
    EnergyProfile(("鶏ひき肉",), 170),
    EnergyProfile(("牛肉", "牛もも", "牛バラ", "牛ロース"), 260),
    EnergyProfile(("豚肉", "豚バラ", "豚ロース", "豚こま"), 240),
    EnergyProfile(("鶏もも",), 190),
    EnergyProfile(("鶏むね", "鶏胸", "ささみ"), 120),
    EnergyProfile(("ひき肉", "挽き肉", "ミンチ"), 230),
    EnergyProfile(("鮭", "サーモン"), 200),
    EnergyProfile(("さば", "サバ"), 210),
    EnergyProfile(("まぐろ", "マグロ", "たら", "タラ", "白身魚"), 120),
    EnergyProfile(("木綿豆腐",), 73),
    EnergyProfile(("絹ごし豆腐", "豆腐"), 56),
    EnergyProfile(("チーズ",), 350),
    EnergyProfile(("生クリーム",), 400, 400),
    EnergyProfile(("牛乳",), 61, 61),
    EnergyProfile(("豆乳",), 45, 45),
    EnergyProfile(("ヨーグルト",), 60, 60),
    EnergyProfile(("卵", "たまご"), 142, kcal_per_unit=(("個", 76), ("玉", 76))),
    EnergyProfile(("じゃがいも", "ジャガイモ", "さつまいも", "サツマイモ"), 90),
    EnergyProfile(("玉ねぎ", "たまねぎ", "タマネギ"), 35, kcal_per_unit=(("個", 65), ("玉", 65))),
    EnergyProfile(("にんじん", "人参", "ニンジン"), 35, kcal_per_unit=(("本", 50),)),
    EnergyProfile(("ブロッコリー",), 37),
    EnergyProfile(("キャベツ", "白菜"), 23),
    EnergyProfile(("トマト",), 20, kcal_per_unit=(("個", 40),)),
    EnergyProfile(("パプリカ", "ピーマン"), 28),
    EnergyProfile(("きのこ", "しめじ", "しいたけ", "椎茸", "えのき", "舞茸", "まいたけ"), 25),
    EnergyProfile(("りんご", "リンゴ"), 55, kcal_per_unit=(("個", 140),)),
    EnergyProfile(("バナナ",), 90, kcal_per_unit=(("本", 85),)),
    EnergyProfile(("醤油", "しょうゆ"), 70, 70),
    EnergyProfile(("みりん",), 240, 240),
    EnergyProfile(("料理酒", "日本酒", "ワイン"), 80, 80),
    EnergyProfile(("味噌", "みそ"), 190),
    EnergyProfile(("ケチャップ",), 105, 105),
    EnergyProfile(("酢",), 25, 25),
    EnergyProfile(("だし", "出汁", "コンソメ", "スープ", "水"), 0, 0),
    EnergyProfile(("塩", "こしょう", "胡椒", "スパイス"), 0, 0),
)

MASS_FACTORS = {"g": 1.0, "グラム": 1.0, "kg": 1000.0, "キログラム": 1000.0}
VOLUME_FACTORS = {
    "ml": 1.0,
    "ミリリットル": 1.0,
    "l": 1000.0,
    "リットル": 1000.0,
    "小さじ": 5.0,
    "大さじ": 15.0,
    "カップ": 200.0,
}
EXACT_KEYWORDS = {"水", "米"}


def _normalized(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _profile(name: str) -> EnergyProfile | None:
    normalized_name = _normalized(name)
    return next(
        (
            profile
            for profile in ENERGY_PROFILES
            if any(
                normalized_name == normalized_keyword
                if normalized_keyword in EXACT_KEYWORDS
                else normalized_keyword in normalized_name
                for keyword in profile.keywords
                if (normalized_keyword := _normalized(keyword))
            )
        ),
        None,
    )


def _ingredient_kcal(name: str, amount: float, unit: str) -> tuple[float, bool] | None:
    profile = _profile(name)
    if profile is None or not math.isfinite(amount) or amount < 0:
        return None
    normalized_unit = _normalized(unit)
    if normalized_unit in MASS_FACTORS and profile.kcal_per_100g is not None:
        return amount * MASS_FACTORS[normalized_unit] * profile.kcal_per_100g / 100, False
    if normalized_unit in VOLUME_FACTORS and profile.kcal_per_100ml is not None:
        return amount * VOLUME_FACTORS[normalized_unit] * profile.kcal_per_100ml / 100, False
    unit_values = dict(profile.kcal_per_unit)
    if normalized_unit in unit_values:
        return amount * unit_values[normalized_unit], True
    return None


def _rounded_kcal(value: float) -> int:
    if value >= 100:
        return int(math.floor((value + 5) / 10) * 10)
    return int(math.floor(value + 0.5))


def estimate_recipe_calories(recipe: dict[str, Any]) -> dict[str, Any]:
    ingredients = recipe.get("ingredients")
    if not isinstance(ingredients, list):
        ingredients = []

    minimum = 0.0
    maximum = 0.0
    estimated_count = 0
    used_unit_assumptions = False
    for ingredient in ingredients:
        if not isinstance(ingredient, dict):
            continue
        raw_amount = ingredient.get("amount")
        if not isinstance(raw_amount, (int, float)) or isinstance(raw_amount, bool):
            continue
        result = _ingredient_kcal(
            str(ingredient.get("name") or ""), float(raw_amount), str(ingredient.get("unit") or "")
        )
        if result is None:
            continue
        item_minimum, assumed_unit = result
        raw_maximum = ingredient.get("amount_max")
        maximum_amount = (
            float(raw_maximum)
            if isinstance(raw_maximum, (int, float)) and not isinstance(raw_maximum, bool)
            else float(raw_amount)
        )
        maximum_result = _ingredient_kcal(
            str(ingredient.get("name") or ""), maximum_amount, str(ingredient.get("unit") or "")
        )
        item_maximum = maximum_result[0] if maximum_result is not None else item_minimum
        minimum += min(item_minimum, item_maximum)
        maximum += max(item_minimum, item_maximum)
        estimated_count += 1
        used_unit_assumptions = used_unit_assumptions or assumed_unit

    has_estimate = estimated_count > 0
    minimum_kcal = _rounded_kcal(minimum) if has_estimate else None
    maximum_kcal = _rounded_kcal(maximum) if has_estimate else None
    servings = recipe.get("servings")
    confirmed_servings = (
        float(servings)
        if recipe.get("servings_confirmed") is not False
        and isinstance(servings, (int, float))
        and not isinstance(servings, bool)
        and math.isfinite(float(servings))
        and float(servings) > 0
        else None
    )
    return {
        "minimum_kcal": minimum_kcal,
        "maximum_kcal": maximum_kcal,
        "per_serving_minimum_kcal": (
            _rounded_kcal(minimum / confirmed_servings)
            if has_estimate and confirmed_servings is not None
            else None
        ),
        "per_serving_maximum_kcal": (
            _rounded_kcal(maximum / confirmed_servings)
            if has_estimate and confirmed_servings is not None
            else None
        ),
        "estimated_ingredient_count": estimated_count,
        "ingredient_count": len(ingredients),
        "has_unestimated_ingredients": estimated_count < len(ingredients),
        "used_unit_assumptions": used_unit_assumptions,
    }


def with_calorie_estimate(recipe: dict[str, Any]) -> dict[str, Any]:
    result = dict(recipe)
    result["calorie_estimate"] = estimate_recipe_calories(result)
    return result

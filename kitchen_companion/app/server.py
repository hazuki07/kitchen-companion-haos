from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sqlite3
import unicodedata
import uuid
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import CombinedRecipeCatalog, MockMealieCatalog, RecipeReader
from .clients import (
    ExternalIntegrationConfig,
    HomeAssistantCookingClient,
    HomeAssistantTimerClient,
    HomeAssistantTodoClient,
    IntegrationError,
    MealieImageClient,
    MealieReadClient,
    MealieSyncClient,
    UrllibJsonTransport,
)
from .integrations import (
    RuntimeConfig,
    StageCookingController,
    StageTimerController,
    StageTodoController,
)
from .nutrition import with_calorie_estimate
from .timing import with_cooking_time_estimate

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MOCK_MEALIE_FIXTURE = APP_DIR / "fixtures" / "mock_mealie.json"
DEFAULT_DB = Path(os.environ.get("COOKING_ASSISTANT_DB", "/data/recipes.sqlite3"))
MAX_BODY = 1_000_000
MAX_IMPORT = 3_000_000
MAX_REVIEW_BUNDLE = 20


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _format_list_number(value: float) -> str:
    return f"{value:g}"


def build_recipe_list_items(
    recipe: dict[str, Any], kind: str, multiplier: float = 1
) -> list[str]:
    if kind not in {"shopping", "tasks"}:
        raise ValueError("リスト種別が正しくありません")
    if not 0.25 <= multiplier <= 20:
        raise ValueError("倍率は0.25倍から20倍で指定してください")
    if kind == "tasks":
        steps = [str(step).strip() for step in recipe.get("steps", []) if str(step).strip()]
        if not steps:
            raise ValueError("工程が登録されていません")
        title = str(recipe.get("title") or "レシピ").strip()
        return [f"{title} — 工程{index}: {step}"[:500] for index, step in enumerate(steps, 1)]

    ingredients = recipe.get("ingredients", [])
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("材料が登録されていません")
    result: list[str] = []
    for ingredient in ingredients[:200]:
        if not isinstance(ingredient, dict):
            continue
        name = str(ingredient.get("name") or "").strip()
        if not name:
            continue
        amount = ingredient.get("amount")
        amount_max = ingredient.get("amount_max")
        unit = str(ingredient.get("unit") or "").strip()
        note = str(ingredient.get("note") or "").strip()
        quantity = ""
        if isinstance(amount, (int, float)):
            low = _format_list_number(float(amount) * multiplier)
            if isinstance(amount_max, (int, float)):
                high = _format_list_number(float(amount_max) * multiplier)
                quantity = f"{low}〜{high}"
            else:
                quantity = low
            if unit:
                quantity = f"{quantity} {unit}"
        elif multiplier == 1:
            quantity = str(ingredient.get("amount_text") or "").strip()
            if quantity and unit:
                quantity = f"{quantity} {unit}"
        details = "・".join(part for part in (quantity, note) if part)
        result.append(f"{name} — {details}"[:500] if details else name[:500])
    if not result:
        raise ValueError("材料が登録されていません")
    return result


class RecipeStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    servings REAL NOT NULL DEFAULT 2 CHECK (servings > 0),
                    servings_confirmed INTEGER NOT NULL DEFAULT 1,
                    source_url TEXT NOT NULL DEFAULT '',
                    video_url TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    portion_kind TEXT NOT NULL DEFAULT 'standard',
                    serving_volume_ml REAL NOT NULL DEFAULT 300,
                    dilution_concentrate_parts REAL NOT NULL DEFAULT 3,
                    dilution_mixer_parts REAL NOT NULL DEFAULT 7,
                    meat_g_per_person REAL NOT NULL DEFAULT 0,
                    rice_go_per_person REAL NOT NULL DEFAULT 0,
                    origin_mealie_slug TEXT NOT NULL DEFAULT '',
                    ingredients_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    step_video_timestamps_json TEXT NOT NULL DEFAULT '[]',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recipes_updated_at
                    ON recipes(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_recipes_title
                    ON recipes(title COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS cooking_action_requests (
                    request_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'outcome_unknown')),
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mealie_sync_records (
                    local_recipe_id TEXT PRIMARY KEY,
                    recipe_fingerprint TEXT NOT NULL,
                    mealie_slug TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'synced', 'outcome_unknown')),
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(recipes)").fetchall()
            }
            migrations = {
                "portion_kind": "TEXT NOT NULL DEFAULT 'standard'",
                "serving_volume_ml": "REAL NOT NULL DEFAULT 300",
                "dilution_concentrate_parts": "REAL NOT NULL DEFAULT 3",
                "dilution_mixer_parts": "REAL NOT NULL DEFAULT 7",
                "meat_g_per_person": "REAL NOT NULL DEFAULT 0",
                "rice_go_per_person": "REAL NOT NULL DEFAULT 0",
                "origin_mealie_slug": "TEXT NOT NULL DEFAULT ''",
                "step_video_timestamps_json": "TEXT NOT NULL DEFAULT '[]'",
                "servings_confirmed": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE recipes ADD COLUMN {column} {definition}")
            count = db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            if count == 0:
                self.save(
                    {
                        "title": "鶏肉と彩り野菜のフライパン蒸し",
                        "description": "平日の夜にも作りやすい、ひとつのフライパンで仕上げるレシピです。",
                        "servings": 2,
                        "image_url": "assets/recipe-hero.png",
                        "ingredients": [
                            {"amount": 300, "unit": "g", "name": "鶏もも肉"},
                            {"amount": 1, "unit": "個", "name": "パプリカ"},
                            {"amount": 0.5, "unit": "株", "name": "ブロッコリー"},
                            {"amount": 1, "unit": "大さじ", "name": "オリーブオイル"},
                            {"amount": None, "unit": "", "name": "塩・こしょう", "note": "適量"},
                        ],
                        "steps": [
                            "鶏肉をひと口大に切り、塩・こしょうを振ります。",
                            "パプリカとブロッコリーを食べやすい大きさに切ります。",
                            "フライパンにオリーブオイルを熱し、鶏肉の表面を焼きます。",
                            "野菜と水大さじ2を加え、ふたをして弱めの中火で7分蒸します。",
                            "鶏肉に火が通ったことを確認し、味を調えます。",
                        ],
                        "tags": ["かんたん", "フライパン", "鶏肉"],
                    }
                )
            db.execute("PRAGMA optimize")

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in (
            "ingredients_json", "steps_json", "step_video_timestamps_json", "tags_json"
        ):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["servings_confirmed"] = bool(result["servings_confirmed"])
        result["source_kind"] = "draft"
        result["read_only"] = False
        return with_cooking_time_estimate(with_calorie_estimate(result))

    def list(self, query: str = "") -> list[dict[str, Any]]:
        with self.connect() as db:
            if query:
                token = f"%{query.strip()}%"
                rows = db.execute(
                    """SELECT recipes.*,
                              mealie_sync_records.status AS mealie_sync_status,
                              mealie_sync_records.mealie_slug AS mealie_slug
                       FROM recipes
                       LEFT JOIN mealie_sync_records
                         ON mealie_sync_records.local_recipe_id = recipes.id
                       WHERE recipes.title LIKE ? OR recipes.description LIKE ?
                          OR recipes.tags_json LIKE ?
                       ORDER BY recipes.updated_at DESC""",
                    (token, token, token),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT recipes.*,
                              mealie_sync_records.status AS mealie_sync_status,
                              mealie_sync_records.mealie_slug AS mealie_slug
                       FROM recipes
                       LEFT JOIN mealie_sync_records
                         ON mealie_sync_records.local_recipe_id = recipes.id
                       ORDER BY recipes.updated_at DESC"""
                ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, recipe_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT recipes.*,
                          mealie_sync_records.status AS mealie_sync_status,
                          mealie_sync_records.mealie_slug AS mealie_slug
                   FROM recipes
                   LEFT JOIN mealie_sync_records
                     ON mealie_sync_records.local_recipe_id = recipes.id
                   WHERE recipes.id = ?""",
                (recipe_id,),
            ).fetchone()
        return self._row(row) if row else None

    def save(self, raw: dict[str, Any], recipe_id: str | None = None) -> dict[str, Any]:
        recipe = validate_recipe(raw)
        now = utc_now()
        recipe_id = recipe_id or str(uuid.uuid4())
        existing = self.get(recipe_id)
        created_at = existing["created_at"] if existing else now
        with self.connect() as db:
            db.execute(
                """INSERT INTO recipes
                   (id, title, description, servings, servings_confirmed,
                    source_url, video_url, image_url,
                    portion_kind, serving_volume_ml, dilution_concentrate_parts,
                    dilution_mixer_parts, meat_g_per_person, rice_go_per_person,
                    ingredients_json, steps_json, step_video_timestamps_json,
                    tags_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    servings=excluded.servings,
                    servings_confirmed=excluded.servings_confirmed,
                    source_url=excluded.source_url,
                    video_url=excluded.video_url, image_url=excluded.image_url,
                    portion_kind=excluded.portion_kind,
                    serving_volume_ml=excluded.serving_volume_ml,
                    dilution_concentrate_parts=excluded.dilution_concentrate_parts,
                    dilution_mixer_parts=excluded.dilution_mixer_parts,
                    meat_g_per_person=excluded.meat_g_per_person,
                    rice_go_per_person=excluded.rice_go_per_person,
                    ingredients_json=excluded.ingredients_json,
                    steps_json=excluded.steps_json,
                    step_video_timestamps_json=excluded.step_video_timestamps_json,
                    tags_json=excluded.tags_json,
                    updated_at=excluded.updated_at""",
                (
                    recipe_id,
                    recipe["title"],
                    recipe["description"],
                    recipe["servings"],
                    int(recipe["servings_confirmed"]),
                    recipe["source_url"],
                    recipe["video_url"],
                    recipe["image_url"],
                    recipe["portion_kind"],
                    recipe["serving_volume_ml"],
                    recipe["dilution_concentrate_parts"],
                    recipe["dilution_mixer_parts"],
                    recipe["meat_g_per_person"],
                    recipe["rice_go_per_person"],
                    json.dumps(recipe["ingredients"], ensure_ascii=False),
                    json.dumps(recipe["steps"], ensure_ascii=False),
                    json.dumps(recipe["step_video_timestamps"], ensure_ascii=False),
                    json.dumps(recipe["tags"], ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
        return self.get(recipe_id)  # type: ignore[return-value]

    def delete(self, recipe_id: str) -> bool:
        with self.connect() as db:
            db.execute("DELETE FROM mealie_sync_records WHERE local_recipe_id = ?", (recipe_id,))
            cursor = db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        return cursor.rowcount > 0

    def set_origin_mealie_slug(self, recipe_id: str, slug: str) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            raise ValueError("Mealie recipe slugが正しくありません")
        with self.connect() as db:
            db.execute(
                "UPDATE recipes SET origin_mealie_slug = ? WHERE id = ?",
                (slug, recipe_id),
            )

    def begin_mealie_sync(
        self, recipe_id: str, fingerprint: str
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT recipe_fingerprint, mealie_slug, status, response_json "
                "FROM mealie_sync_records WHERE local_recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row:
                response = json.loads(row["response_json"]) if row["response_json"] else None
                if row["status"] == "synced" and row["recipe_fingerprint"] == fingerprint:
                    return "synced", row["mealie_slug"], response
                if row["status"] in {"pending", "outcome_unknown"}:
                    return str(row["status"]), row["mealie_slug"], response
                db.execute(
                    "UPDATE mealie_sync_records SET recipe_fingerprint = ?, status = 'pending', "
                    "response_json = NULL, updated_at = ? WHERE local_recipe_id = ?",
                    (fingerprint, now, recipe_id),
                )
                return "update", row["mealie_slug"], None
            origin = db.execute(
                "SELECT origin_mealie_slug FROM recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()
            origin_slug = str(origin["origin_mealie_slug"] or "") if origin else ""
            if origin_slug:
                db.execute(
                    "INSERT INTO mealie_sync_records "
                    "(local_recipe_id, recipe_fingerprint, mealie_slug, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (recipe_id, fingerprint, origin_slug, now, now),
                )
                return "update", origin_slug, None
            db.execute(
                "INSERT INTO mealie_sync_records "
                "(local_recipe_id, recipe_fingerprint, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (recipe_id, fingerprint, now, now),
            )
        return "create", None, None

    def set_mealie_slug(self, recipe_id: str, slug: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE mealie_sync_records SET mealie_slug = ?, updated_at = ? "
                "WHERE local_recipe_id = ? AND status = 'pending'",
                (slug, utc_now(), recipe_id),
            )

    def finish_mealie_sync(self, recipe_id: str, response: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE mealie_sync_records SET status = 'synced', response_json = ?, updated_at = ? "
                "WHERE local_recipe_id = ?",
                (json.dumps(response, ensure_ascii=False), utc_now(), recipe_id),
            )

    def mark_mealie_sync_unknown(self, recipe_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE mealie_sync_records SET status = 'outcome_unknown', updated_at = ? "
                "WHERE local_recipe_id = ?",
                (utc_now(), recipe_id),
            )

    def mealie_sync_state(self, recipe_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT mealie_slug, status FROM mealie_sync_records "
                "WHERE local_recipe_id = ?",
                (recipe_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_mealie_sync_verification(
        self, recipe_id: str, mealie_slug: str, matched: bool
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, mealie_slug FROM mealie_sync_records "
                "WHERE local_recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if (
                not row
                or row["status"] != "outcome_unknown"
                or str(row["mealie_slug"] or "") != mealie_slug
            ):
                raise ValueError("確認対象のMealie同期状態が見つかりません")
            if matched:
                response = json.dumps(
                    {"status": "synced", "mealie_slug": mealie_slug},
                    ensure_ascii=False,
                )
                db.execute(
                    "UPDATE mealie_sync_records SET status = 'synced', response_json = ?, "
                    "updated_at = ? WHERE local_recipe_id = ?",
                    (response, utc_now(), recipe_id),
                )
                return
            db.execute(
                "UPDATE recipes SET origin_mealie_slug = ? WHERE id = ?",
                (mealie_slug, recipe_id),
            )
            db.execute(
                "DELETE FROM mealie_sync_records WHERE local_recipe_id = ?",
                (recipe_id,),
            )

    def begin_action(self, request_id: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_fingerprint, status, response_json "
                "FROM cooking_action_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                if row["request_fingerprint"] != fingerprint:
                    raise ValueError("同じrequest IDへ異なる操作は指定できません")
                response = json.loads(row["response_json"]) if row["response_json"] else None
                return str(row["status"]), response
            db.execute(
                "INSERT INTO cooking_action_requests "
                "(request_id, request_fingerprint, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (request_id, fingerprint, now, now),
            )
        return "new", None

    def finish_action(self, request_id: str, response: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE cooking_action_requests "
                "SET status = 'completed', response_json = ?, updated_at = ? WHERE request_id = ?",
                (json.dumps(response, ensure_ascii=False), utc_now(), request_id),
            )

    def mark_action_unknown(self, request_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE cooking_action_requests "
                "SET status = 'outcome_unknown', updated_at = ? WHERE request_id = ?",
                (utc_now(), request_id),
            )

    def action_status(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT status, response_json FROM cooking_action_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if not row:
            return None
        result: dict[str, Any] = {"request_id": request_id, "status": str(row["status"])}
        if row["response_json"]:
            result["result"] = json.loads(row["response_json"])
        return result


def parse_video_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("工程動画の開始位置を正しく入力してください")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            seconds = int(float(text))
        else:
            match = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,3}):(\d{2})", text)
            if not match:
                raise ValueError("工程動画の開始位置は mm:ss または hh:mm:ss で入力してください")
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2))
            tail_seconds = int(match.group(3))
            if tail_seconds >= 60 or (match.group(1) and minutes >= 60):
                raise ValueError("工程動画の開始位置を正しく入力してください")
            seconds = hours * 3600 + minutes * 60 + tail_seconds
    if not 0 <= seconds <= 86_400:
        raise ValueError("工程動画の開始位置は24時間以内にしてください")
    return seconds


def steps_with_video_timestamps(steps: list[str]) -> tuple[list[str], list[int | None]]:
    clean_steps: list[str] = []
    timestamps: list[int | None] = []
    pattern = re.compile(r"^\s*\[?((?:\d{1,2}:)?\d{1,3}:\d{2})\]?\s*[-–—]?\s*(.+)$")
    for step in steps:
        match = pattern.match(step)
        if match:
            timestamps.append(parse_video_timestamp(match.group(1)))
            clean_steps.append(match.group(2).strip())
        else:
            timestamps.append(None)
            clean_steps.append(step)
    return clean_steps, timestamps


def validate_recipe(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("レシピの形式が正しくありません")
    title = str(raw.get("title", "")).strip()
    if not title or len(title) > 160:
        raise ValueError("料理名を160文字以内で入力してください")
    try:
        servings = float(raw.get("servings", 2))
    except (TypeError, ValueError) as exc:
        raise ValueError("人数を正しく入力してください") from exc
    if not 0 < servings <= 100:
        raise ValueError("人数は0より大きく100以下にしてください")
    servings_confirmed = raw.get("servings_confirmed", True)
    if not isinstance(servings_confirmed, bool):
        raise ValueError("基準人数の確認状態が正しくありません")
    portion_kind = str(raw.get("portion_kind") or "standard")
    portion_kinds = {"standard", "meat_main", "mixed_meat", "drink", "diluted_drink"}
    if portion_kind not in portion_kinds:
        raise ValueError("分量タイプが正しくありません")

    def portion_number(key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError("1人分の目安量を正しく入力してください") from exc
        if not minimum <= value <= maximum:
            raise ValueError("1人分の目安量が範囲外です")
        return value

    default_meat = 200 if portion_kind == "meat_main" else 150 if portion_kind == "mixed_meat" else 0
    serving_volume_ml = portion_number("serving_volume_ml", 300, 50, 2000)
    dilution_concentrate_parts = portion_number("dilution_concentrate_parts", 3, 0.1, 100)
    dilution_mixer_parts = portion_number("dilution_mixer_parts", 7, 0.1, 100)
    meat_g_per_person = portion_number("meat_g_per_person", default_meat, 0, 1000)
    rice_go_per_person = portion_number("rice_go_per_person", 0, 0, 5)
    ingredients = raw.get("ingredients", [])
    steps = raw.get("steps", [])
    step_video_timestamps = raw.get("step_video_timestamps", [])
    tags = raw.get("tags", [])
    if (
        not isinstance(ingredients, list)
        or not isinstance(steps, list)
        or not isinstance(step_video_timestamps, list)
        or not isinstance(tags, list)
    ):
        raise ValueError("材料、工程、タグの形式が正しくありません")
    clean_ingredients = []
    for item in ingredients[:200]:
        if isinstance(item, str):
            clean_ingredients.append(parse_ingredient(item))
        elif isinstance(item, dict) and str(item.get("name", "")).strip():
            amount = item.get("amount")
            if amount not in (None, ""):
                try:
                    amount = float(amount)
                except (TypeError, ValueError):
                    amount = None
            else:
                amount = None
            amount_max = item.get("amount_max")
            if amount_max not in (None, ""):
                try:
                    amount_max = float(amount_max)
                except (TypeError, ValueError):
                    amount_max = None
            else:
                amount_max = None
            if amount is None or (amount_max is not None and amount_max < amount):
                amount_max = None
            amount_text = str(item.get("amount_text", "")).strip()[:50]
            if amount is not None and not amount_text:
                amount_text = f"{amount:g}"
            unit = str(item.get("unit", ""))[:30]
            name = str(item["name"]).strip()[:200]
            note = str(item.get("note", ""))[:200]
            original_text = str(item.get("original_text", "")).strip()[:500]
            if not original_text:
                original_text = " ".join(
                    part for part in (amount_text, unit, name, note) if part
                )[:500]
            clean_ingredients.append(
                {
                    "amount": amount,
                    "amount_max": amount_max,
                    "amount_text": amount_text,
                    "unit": unit,
                    "name": name,
                    "note": note,
                    "original_text": original_text,
                }
            )
    clean_steps: list[str] = []
    clean_timestamps: list[int | None] = []
    for index, step in enumerate(steps[:100]):
        clean_step = str(step).strip()[:2000]
        if not clean_step:
            continue
        clean_steps.append(clean_step)
        clean_timestamps.append(
            parse_video_timestamp(step_video_timestamps[index])
            if index < len(step_video_timestamps)
            else None
        )
    return {
        "title": title,
        "description": str(raw.get("description", "")).strip()[:2000],
        "servings": servings,
        "servings_confirmed": servings_confirmed,
        "source_url": safe_http_url(raw.get("source_url", ""), allow_empty=True),
        "video_url": safe_http_url(raw.get("video_url", ""), allow_empty=True),
        "image_url": safe_image_url(raw.get("image_url", "")),
        "portion_kind": portion_kind,
        "serving_volume_ml": serving_volume_ml,
        "dilution_concentrate_parts": dilution_concentrate_parts,
        "dilution_mixer_parts": dilution_mixer_parts,
        "meat_g_per_person": meat_g_per_person,
        "rice_go_per_person": rice_go_per_person,
        "ingredients": clean_ingredients,
        "steps": clean_steps,
        "step_video_timestamps": clean_timestamps,
        "tags": [str(tag).strip()[:50] for tag in tags[:30] if str(tag).strip()],
    }


def safe_http_url(value: Any, allow_empty: bool = False) -> str:
    value = str(value or "").strip()
    if not value and allow_empty:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("httpまたはhttpsのURLを入力してください")
    return value[:2000]


def safe_image_url(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("/assets/") or value.startswith("assets/"):
        return value.lstrip("/")
    return safe_http_url(value)


def create_normalized_draft(
    store: RecipeStore, recipe: dict[str, Any], mealie_slug: str
) -> dict[str, Any]:
    if recipe.get("servings_confirmed") is False:
        raise ValueError("基準人数を確認してから正規化してください")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", mealie_slug):
        raise ValueError("Mealie recipe slugが正しくありません")
    draft_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kitchen-companion:mealie:{mealie_slug}"))
    existing = store.get(draft_id)
    if existing and existing.get("mealie_sync_status") != "synced":
        return existing
    image_url = str(recipe.get("image_url") or "")
    if image_url and not (
        image_url.startswith(("assets/", "/assets/", "http://", "https://"))
    ):
        image_url = ""
    fields = (
        "title", "description", "servings", "source_url", "video_url",
        "portion_kind", "serving_volume_ml", "dilution_concentrate_parts",
        "dilution_mixer_parts", "meat_g_per_person", "rice_go_per_person",
        "ingredients", "steps", "step_video_timestamps", "tags",
    )
    draft = store.save(
        {
            **{key: recipe[key] for key in fields if recipe.get(key) is not None},
            "image_url": image_url,
        },
        draft_id,
    )
    store.set_origin_mealie_slug(draft_id, mealie_slug)
    return store.get(draft_id) or draft


def ingredient_amounts_match(local: dict[str, Any], remote: dict[str, Any]) -> bool:
    def semantic(recipe: dict[str, Any]) -> list[tuple[Any, ...]]:
        result = []
        for item in recipe.get("ingredients", []):
            if not isinstance(item, dict):
                continue
            result.append(
                (
                    item.get("amount"),
                    item.get("amount_max"),
                    str(item.get("unit") or "").strip().casefold(),
                    str(item.get("name") or "").strip().casefold(),
                    str(item.get("note") or "").strip().casefold(),
                )
            )
        return result

    return semantic(local) == semantic(remote)


def canonical_recipe_source(recipe: dict[str, Any]) -> str:
    for key in ("video_url", "source_url"):
        raw_url = str(recipe.get(key) or "").strip()
        if not raw_url:
            continue
        try:
            parsed = urlparse(raw_url)
        except ValueError:
            continue
        hostname = (parsed.hostname or "").casefold()
        hostname = hostname.removeprefix("www.").removeprefix("m.")
        video_id = ""
        if hostname == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
        elif hostname in {"youtube.com", "youtube-nocookie.com"}:
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if not video_id:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[0] in {"embed", "live", "shorts"}:
                    video_id = parts[1]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return f"youtube:{video_id}"
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            try:
                port_number = parsed.port
            except ValueError:
                continue
            port = f":{port_number}" if port_number else ""
            path = parsed.path.rstrip("/") or "/"
            return f"url:{parsed.scheme.casefold()}://{hostname}{port}{path}"
    return ""


def append_unresolved(unresolved: list[str], message: str) -> None:
    if len(unresolved) < 20 and message not in unresolved:
        unresolved.append(message)


def _review_bundle_payload(
    raw: dict[str, Any], catalog: RecipeReader
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recipes = raw.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise ValueError("レビュー済みレシピを1件以上指定してください")
    if len(recipes) > MAX_REVIEW_BUNDLE:
        raise ValueError(f"一度に保存できるレシピは{MAX_REVIEW_BUNDLE}件までです")

    existing_by_source: dict[str, dict[str, Any]] = {}
    for existing in catalog.list():
        identity = canonical_recipe_source(existing)
        if identity:
            existing_by_source.setdefault(identity, existing)

    entries: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, raw_recipe in enumerate(recipes, start=1):
        if not isinstance(raw_recipe, dict):
            raise ValueError(f"{index}件目のレシピ形式が正しくありません")
        recipe = validate_recipe(raw_recipe)
        identity = canonical_recipe_source(recipe)
        if not identity:
            raise ValueError(f"{index}件目に有効な出典URLまたは動画URLがありません")
        unresolved = raw_recipe.get("unresolved_fields", [])
        if not isinstance(unresolved, list):
            raise ValueError(f"{index}件目の未確認項目の形式が正しくありません")
        unresolved = [str(item)[:300] for item in unresolved[:20] if str(item).strip()]

        if not recipe["servings_confirmed"] and not any("基準人数" in item for item in unresolved):
            unresolved.insert(0, "基準人数は未確認")
            unresolved = unresolved[:20]
        if not recipe["ingredients"]:
            append_unresolved(unresolved, "材料を取得できていません")
        elif recipe["video_url"] and raw_recipe.get("ingredients_complete") is not True:
            append_unresolved(unresolved, "概要欄の全材料を確認済みか未確認")
        if not recipe["steps"]:
            append_unresolved(unresolved, "工程を取得できていません")
        elif recipe["video_url"] and raw_recipe.get("steps_complete") is not True:
            append_unresolved(unresolved, "動画の完成まで工程を確認済みか未確認")
        if unresolved:
            review_note = f"未確認項目: {'／'.join(unresolved)}"
            recipe["description"] = "\n\n".join(
                part for part in (recipe["description"], review_note) if part
            )[:2000]
            if "要確認" not in recipe["tags"]:
                recipe["tags"] = [*recipe["tags"], "要確認"][:30]
        entry = {
            "title": recipe["title"],
            "source_url": recipe["video_url"] or recipe["source_url"],
            "servings": recipe["servings"],
            "servings_confirmed": recipe["servings_confirmed"],
            "ingredient_count": len(recipe["ingredients"]),
            "step_count": len(recipe["steps"]),
            "unresolved_fields": unresolved,
        }
        duplicate = existing_by_source.get(identity)
        if duplicate:
            entry.update(
                disposition="duplicate",
                duplicate_title=str(duplicate.get("title") or "既存レシピ"),
            )
        elif identity in seen_sources:
            entry.update(
                disposition="duplicate",
                duplicate_title="同じバンドル内のレシピ",
            )
        else:
            entry.update(disposition="candidate", duplicate_title="")
            candidates.append(recipe)
            seen_sources.add(identity)
        entries.append(entry)

    counts = {
        "total": len(entries),
        "candidate": sum(entry["disposition"] == "candidate" for entry in entries),
        "duplicate": sum(entry["disposition"] == "duplicate" for entry in entries),
        "unresolved": sum(bool(entry["unresolved_fields"]) for entry in entries),
    }
    token_payload = {
        "entries": entries,
        "candidates": candidates,
    }
    review_token = hashlib.sha256(
        json.dumps(
            token_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "entries": entries,
        "counts": counts,
        "review_token": review_token,
    }, candidates


def preview_review_bundle(raw: dict[str, Any], catalog: RecipeReader) -> dict[str, Any]:
    review, _ = _review_bundle_payload(raw, catalog)
    return review


def save_review_bundle(
    raw: dict[str, Any], catalog: RecipeReader, store: RecipeStore
) -> dict[str, Any]:
    review, candidates = _review_bundle_payload(raw, catalog)
    review_token = str(raw.get("review_token") or "")
    if not review_token or not hmac.compare_digest(review_token, review["review_token"]):
        raise ValueError("プレビュー結果が変わりました。もう一度内容を確認してください")
    try:
        confirm_count = int(raw.get("confirm_count"))
    except (TypeError, ValueError) as exc:
        raise ValueError("保存件数の確認が必要です") from exc
    if confirm_count != len(candidates):
        raise ValueError("確認した保存件数と候補数が一致しません")
    if not candidates:
        raise ValueError("新しく保存するレシピはありません")

    saved_titles: list[str] = []
    for recipe in candidates:
        identity = canonical_recipe_source(recipe)
        recipe_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kitchen-companion:{identity}"))
        store.save(recipe, recipe_id)
        saved_titles.append(recipe["title"])
    return {
        "saved_count": len(saved_titles),
        "saved_titles": saved_titles,
        "duplicate_count": review["counts"]["duplicate"],
    }


def parse_ingredient(text: str) -> dict[str, Any]:
    original_text = str(text).strip()[:500]
    normalized = unicodedata.normalize("NFKC", original_text)
    normalized = normalized.replace("½", "1/2").replace("¼", "1/4").replace("¾", "3/4")
    normalized = re.sub(r"^[\s・●◯○■□✓✔*\-]+", "", normalized).strip()
    number = r"\d+\s*/\s*\d+|\d+(?:\.\d+)?"
    amount_pattern = rf"(?P<low>{number})(?:\s*(?:〜|~|－|-)\s*(?P<high>{number}))?"
    unit_pattern = (
        r"(?P<unit>大さじ|小さじ|カップ|cc|ml|mL|l|L|g|kg|個|本|枚|片|株|袋|缶|"
        r"束|合|玉|丁|パック|つまみ)"
    )
    separator = r"[\s:：・…]*"
    note_suffix = r"(?:\s*[（(](?P<note>[^）)]{1,100})[）)])?"
    patterns = (
        rf"^(?P<name>.+?){separator}{unit_pattern}\s*{amount_pattern}{note_suffix}\s*$",
        rf"^{amount_pattern}\s*{unit_pattern}?{separator}(?P<name>.+?){note_suffix}\s*$",
        rf"^(?P<name>.+?){separator}{amount_pattern}\s*{unit_pattern}{note_suffix}\s*$",
    )
    match = next((candidate for pattern in patterns if (candidate := re.match(pattern, normalized))), None)
    if not match:
        qualitative = re.match(
            r"^(?P<name>.+?)[\s:：・…]*(?P<note>適量|少々|ひとつまみ|お好みで)\s*$",
            normalized,
        )
        return {
            "amount": None,
            "amount_max": None,
            "amount_text": "",
            "unit": "",
            "name": (qualitative.group("name") if qualitative else normalized)[:200].strip(),
            "note": (qualitative.group("note") if qualitative else ""),
            "original_text": original_text,
        }

    def numeric(value: str) -> float:
        value = value.replace(" ", "")
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)

    low_text = match.group("low").replace(" ", "")
    high_text = (match.groupdict().get("high") or "").replace(" ", "")
    return {
        "amount": numeric(low_text),
        "amount_max": numeric(high_text) if high_text else None,
        "amount_text": low_text[:50],
        "unit": (match.groupdict().get("unit") or ""),
        "name": match.group("name")[:200].strip(" :：・…"),
        "note": (match.groupdict().get("note") or "").strip(),
        "original_text": original_text,
    }


class JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.buffer: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "script" and "ld+json" in (values.get("type") or "").lower():
            self.capture = True
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.capture:
            self.blocks.append("".join(self.buffer))
            self.capture = False


class PageMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.metadata: dict[str, str] = {}
        self.capture_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "title":
            self.capture_title = True
        if tag.lower() != "meta":
            return
        key = str(values.get("property") or values.get("name") or "").casefold()
        content = str(values.get("content") or "").strip()
        if key and content:
            self.metadata.setdefault(key, content)

    def handle_data(self, data: str) -> None:
        if self.capture_title and data.strip():
            self.title_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.capture_title = False

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()


def find_typed_node(value: Any, node_type: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        kinds = value.get("@type", [])
        if isinstance(kinds, str):
            kinds = [kinds]
        elif not isinstance(kinds, list):
            kinds = []
        if node_type in kinds:
            return value
        for nested in value.values():
            found = find_typed_node(nested, node_type)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = find_typed_node(nested, node_type)
            if found:
                return found
    return None


def find_recipe_node(value: Any) -> dict[str, Any] | None:
    return find_typed_node(value, "Recipe")


def flatten_steps(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        if value.get("text"):
            return [str(value["text"]).strip()]
        return flatten_steps(value.get("itemListElement", []))
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(flatten_steps(item))
        return result
    return []


def extract_video_url(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            if url := extract_video_url(item):
                return url
        return ""
    if isinstance(value, dict):
        for key in ("contentUrl", "embedUrl", "url"):
            if url := extract_video_url(value.get(key)):
                return url
        return ""
    try:
        return safe_http_url(value, allow_empty=True)
    except ValueError:
        return ""


def extract_description_recipe(description: str) -> tuple[float, list[dict[str, Any]], list[str]]:
    description = unicodedata.normalize("NFKC", str(description or ""))
    servings_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:人分|servings?)", description, re.IGNORECASE
    )
    servings = float(servings_match.group(1)) if servings_match else 2
    ingredients: list[dict[str, Any]] = []
    steps: list[str] = []
    section = ""
    heading_prefix = r"[\s#【\[（(■□●○◆◇▼▽★☆*=_\-]*"
    ingredient_heading = re.compile(
        rf"^{heading_prefix}(?:材料(?:一覧)?|食材|調味料|ingredients?)",
        re.IGNORECASE,
    )
    step_heading = re.compile(
        rf"^{heading_prefix}(?:作り方|作りかた|手順|調理手順|工程|directions?|"
        r"instructions?|method|how\s+to\s+make)",
        re.IGNORECASE,
    )
    unrelated_heading = re.compile(
        rf"^{heading_prefix}(?:ポイント|コツ|注意|保存|目次|チャプター|chapter|"
        r"timestamps?|関連|リンク|sns|お知らせ|道具|器具|equipment|notes?|tips?)",
        re.IGNORECASE,
    )
    decoration_heading = re.compile(r"^[【\[（(].{1,40}[】\]）)]\s*$")
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if ingredient_heading.match(line):
            section = "ingredients"
            continue
        if step_heading.match(line):
            section = "steps"
            continue
        if unrelated_heading.match(line):
            section = ""
            continue
        if not line or not section:
            continue
        if line.startswith(("http://", "https://")) or (line.startswith("#") and len(line) < 80):
            section = ""
            continue
        if decoration_heading.match(line):
            continue
        cleaned = re.sub(r"^[\s・●◯○■□✓✔*-]+", "", line)
        if section == "steps":
            cleaned = re.sub(
                r"^(?:step\s*)?\d+[.)．、:](?!\d)\s*", "", cleaned, flags=re.IGNORECASE
            )
        if not cleaned:
            continue
        if section == "ingredients" and len(ingredients) < 200:
            ingredients.append(parse_ingredient(cleaned))
        elif section == "steps" and len(steps) < 100:
            steps.append(cleaned[:2000])
    return servings, ingredients, steps


def assert_public_host(url: str) -> None:
    hostname = urlparse(url).hostname
    if not hostname:
        raise ValueError("URLのホスト名がありません")
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError("サイトのアドレスを確認できません") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("ローカルネットワークのURLは取り込めません")


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        safe_http_url(newurl)
        assert_public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public_html(url: str, kind: str) -> tuple[str, str]:
    url = safe_http_url(url)
    assert_public_host(url)
    request = Request(url, headers={"User-Agent": "KitchenCompanion/0.1 (+personal recipe importer)"})
    try:
        with build_opener(SafeRedirectHandler()).open(request, timeout=12) as response:
            final_url = response.geturl()
            assert_public_host(final_url)
            content_type = response.headers.get_content_type()
            if content_type != "text/html":
                raise ValueError("HTMLのレシピページを指定してください")
            raw = response.read(MAX_IMPORT + 1)
            if len(raw) > MAX_IMPORT:
                raise ValueError("ページが大きすぎて取り込めません")
            charset = response.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ValueError(f"{kind}ページを取得できませんでした") from exc
    return url, html


def parse_json_ld_nodes(html: str) -> list[Any]:
    parser = JsonLdParser()
    parser.feed(html)
    nodes = []
    for block in parser.blocks:
        try:
            nodes.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    return nodes


def extract_embedded_json(html: str, marker: str) -> dict[str, Any] | None:
    """Read a JSON object assigned to a known page variable without evaluating JavaScript."""
    decoder = json.JSONDecoder()
    for found in re.finditer(re.escape(marker), html):
        object_start = html.find("{", found.end(), found.end() + 500)
        if object_start < 0:
            continue
        try:
            value, _ = decoder.raw_decode(html[object_start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _structured_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    if value.get("simpleText"):
        return str(value["simpleText"]).strip()
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(
            str(run.get("text") or "") for run in runs if isinstance(run, dict)
        ).strip()
    return str(value.get("content") or "").strip()


def youtube_player_metadata(html: str) -> dict[str, str]:
    response = extract_embedded_json(html, "ytInitialPlayerResponse") or {}
    details = response.get("videoDetails")
    details = details if isinstance(details, dict) else {}
    microformat = response.get("microformat")
    microformat = microformat if isinstance(microformat, dict) else {}
    renderer = microformat.get("playerMicroformatRenderer")
    renderer = renderer if isinstance(renderer, dict) else {}

    descriptions = [
        str(details.get("shortDescription") or "").strip(),
        _structured_text(renderer.get("description")),
    ]
    thumbnail_data = details.get("thumbnail")
    thumbnail_data = thumbnail_data if isinstance(thumbnail_data, dict) else {}
    thumbnails = thumbnail_data.get("thumbnails", [])
    thumbnails = thumbnails if isinstance(thumbnails, list) else []
    image = next(
        (
            str(item.get("url") or "")
            for item in reversed(thumbnails)
            if isinstance(item, dict) and item.get("url")
        ),
        "",
    )
    return {
        "title": str(details.get("title") or "").strip() or _structured_text(renderer.get("title")),
        "description": max(descriptions, key=len, default=""),
        "image": image,
    }


def recipe_draft_from_node(node: dict[str, Any], source_url: str, video_url: str = "") -> dict[str, Any]:
    image = node.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    yield_value = node.get("recipeYield")
    if isinstance(yield_value, list):
        yield_value = yield_value[0] if yield_value else None
    match = re.search(r"\d+(?:\.\d+)?", str(yield_value))
    servings = float(match.group()) if match else 2
    steps, step_video_timestamps = steps_with_video_timestamps(
        flatten_steps(node.get("recipeInstructions", []))
    )
    return validate_recipe(
        {
            "title": node.get("name", "取り込んだレシピ"),
            "description": node.get("description", ""),
            "servings": servings,
            "servings_confirmed": match is not None,
            "source_url": source_url,
            "video_url": video_url or extract_video_url(node.get("video")),
            "image_url": urljoin(source_url, str(image)) if image else "",
            "ingredients": [parse_ingredient(item) for item in node.get("recipeIngredient", [])],
            "steps": steps,
            "step_video_timestamps": step_video_timestamps,
            "tags": ["動画"] if video_url else [],
        }
    )


def with_video_import_warnings(recipe: dict[str, Any]) -> dict[str, Any]:
    result = dict(recipe)
    warnings: list[str] = []
    if not result.get("ingredients"):
        warnings.append("概要欄または構造化データから材料を確認できませんでした")
    if not result.get("steps"):
        warnings.append(
            "概要欄または構造化データに作り方がありません。動画音声は自動要約していないため、工程を確認してください"
        )
    result["import_warnings"] = warnings
    return result


def video_draft_from_html(url: str, html: str) -> dict[str, Any]:
    structured = parse_json_ld_nodes(html)
    recipe_node = next((node for value in structured if (node := find_recipe_node(value))), None)
    youtube = youtube_player_metadata(html)
    if recipe_node:
        draft = recipe_draft_from_node(recipe_node, url, url)
        full_description = youtube["description"]
        servings, ingredients, steps = extract_description_recipe(full_description)
        if not draft["ingredients"] and ingredients:
            draft["ingredients"] = ingredients
        if not draft["steps"] and steps:
            clean_steps, timestamps = steps_with_video_timestamps(steps)
            draft["steps"] = clean_steps
            draft["step_video_timestamps"] = timestamps
        if full_description and len(full_description) > len(draft["description"]):
            draft["description"] = full_description[:2000]
        if not draft["servings_confirmed"] and re.search(
            r"\d+(?:\.\d+)?\s*(?:人分|servings?)", full_description, re.IGNORECASE
        ):
            draft["servings"] = servings
            draft["servings_confirmed"] = True
        return with_video_import_warnings(draft)

    video_node = next(
        (node for value in structured if (node := find_typed_node(value, "VideoObject"))),
        None,
    )
    metadata = PageMetadataParser()
    metadata.feed(html)
    title = youtube["title"] or str((video_node or {}).get("name") or "").strip()
    if not title:
        title = metadata.metadata.get("og:title") or metadata.metadata.get("twitter:title") or metadata.title
    description_candidates = [
        youtube["description"],
        str((video_node or {}).get("description") or "").strip(),
        metadata.metadata.get("og:description", ""),
        metadata.metadata.get("twitter:description", ""),
        metadata.metadata.get("description", ""),
    ]
    description = max(description_candidates, key=len, default="")
    image = youtube["image"] or (video_node or {}).get("thumbnailUrl", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if not image:
        image = metadata.metadata.get("og:image") or metadata.metadata.get("twitter:image") or ""
    servings, ingredients, steps = extract_description_recipe(description)
    servings_confirmed = bool(
        re.search(r"\d+(?:\.\d+)?\s*(?:人分|servings?)", description, re.IGNORECASE)
    )
    steps, step_video_timestamps = steps_with_video_timestamps(steps)
    try:
        image_url = safe_image_url(urljoin(url, str(image or "")))
    except ValueError:
        image_url = ""
    draft = validate_recipe(
        {
            "title": title or "動画レシピ",
            "description": description,
            "servings": servings,
            "servings_confirmed": servings_confirmed,
            "source_url": url,
            "video_url": url,
            "image_url": image_url,
            "ingredients": ingredients,
            "steps": steps,
            "step_video_timestamps": step_video_timestamps,
            "tags": ["動画"],
        }
    )
    return with_video_import_warnings(draft)


def import_recipe(url: str) -> dict[str, Any]:
    url, html = fetch_public_html(url, "レシピサイト")
    node = None
    for value in parse_json_ld_nodes(html):
        node = find_recipe_node(value)
        if node:
            break
    if not node:
        raise ValueError("このページからレシピ情報を見つけられませんでした")
    return recipe_draft_from_node(node, url)


def import_video(url: str) -> dict[str, Any]:
    url, html = fetch_public_html(url, "動画")
    return video_draft_from_html(url, html)


class AppHandler(BaseHTTPRequestHandler):
    store: RecipeStore
    catalog: RecipeReader
    runtime: RuntimeConfig
    cooking: StageCookingController | HomeAssistantCookingClient
    timer: StageTimerController | HomeAssistantTimerClient
    todo: StageTodoController | HomeAssistantTodoClient
    images: MealieImageClient | None
    mealie_sync: MealieSyncClient | None

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def json_response(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def image_response(self, content_type: str, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def ingress_allowed(self) -> bool:
        if os.environ.get("TRUST_HOME_ASSISTANT_INGRESS") != "1":
            return True
        if self.client_address[0] == "172.30.32.2":
            return True
        self.json_response({"error": "Home Assistant Ingressから開いてください"}, 403)
        return False

    def body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("リクエストの長さが正しくありません") from exc
        if not 0 < length <= MAX_BODY:
            raise ValueError("送信データが大きすぎます")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("JSONの形式が正しくありません") from exc
        if not isinstance(value, dict):
            raise ValueError("データの形式が正しくありません")
        return value

    def do_GET(self) -> None:
        if not self.ingress_allowed():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.json_response({"status": "ok"})
            return
        if parsed.path == "/api/runtime":
            self.json_response(self.runtime.public_status())
            return
        if parsed.path == "/api/recipes":
            query = parse_qs(parsed.query).get("q", [""])[0][:160]
            self.json_response({"recipes": self.store.list(query)})
            return
        if parsed.path == "/api/catalog/recipes":
            query = parse_qs(parsed.query).get("q", [""])[0][:160]
            try:
                recipes = self.catalog.list(query)
            except IntegrationError:
                self.json_response(
                    {
                        "recipes": self.store.list(query),
                        "mealie_status": "error",
                        "mealie_count": 0,
                        "mealie_message": (
                            "Mealieへ接続できません。App設定のmode、Mealie URL、tokenを確認してください。"
                        ),
                    }
                )
                return
            self.json_response(
                {
                    "recipes": recipes,
                    "mealie_status": "connected" if self.runtime.mode == "home_assistant" else "mock",
                    "mealie_count": sum(
                        recipe.get("source_kind") == "mealie" for recipe in recipes
                    ),
                }
            )
            return
        image_match = re.fullmatch(r"/api/catalog/images/([a-fA-F0-9-]{8,64})", parsed.path)
        if image_match:
            if not self.images:
                self.json_response({"error": "画像はStageモックでは利用できません"}, 404)
                return
            try:
                content_type, payload = self.images.get(image_match.group(1))
                self.image_response(content_type, payload)
            except (ValueError, IntegrationError):
                self.json_response({"error": "画像を取得できませんでした"}, 502)
            return
        action_match = re.fullmatch(r"/api/cooking/requests/([0-9a-f-]{36})", parsed.path)
        if action_match:
            status = self.store.action_status(action_match.group(1))
            self.json_response(status or {"error": "操作が見つかりません"}, 200 if status else 404)
            return
        catalog_match = re.fullmatch(r"/api/catalog/recipes/([a-z0-9-]+)", parsed.path)
        if catalog_match:
            recipe = self.catalog.get(catalog_match.group(1))
            self.json_response(recipe or {"error": "レシピが見つかりません"}, 200 if recipe else 404)
            return
        match = re.fullmatch(r"/api/recipes/([0-9a-f-]+)", parsed.path)
        if match:
            recipe = self.store.get(match.group(1))
            self.json_response(recipe or {"error": "レシピが見つかりません"}, 200 if recipe else 404)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        if not self.ingress_allowed():
            return
        try:
            normalize_match = re.fullmatch(
                r"/api/catalog/recipes/([a-zA-Z0-9_-]+)/normalize", self.path
            )
            if normalize_match:
                mealie_slug = normalize_match.group(1)
                recipe = self.catalog.get(mealie_slug)
                if not recipe or recipe.get("source_kind") != "mealie":
                    self.json_response({"error": "正規化するMealieレシピが見つかりません"}, 404)
                    return
                draft = create_normalized_draft(self.store, recipe, mealie_slug)
                self.json_response(draft, HTTPStatus.CREATED)
            elif self.path == "/api/recipes":
                self.json_response(self.store.save(self.body()), HTTPStatus.CREATED)
            elif self.path == "/api/import":
                data = self.body()
                self.json_response(import_recipe(str(data.get("url", ""))))
            elif self.path == "/api/import/video":
                data = self.body()
                self.json_response(import_video(str(data.get("url", ""))))
            elif self.path == "/api/import/bundle/preview":
                self.json_response(preview_review_bundle(self.body(), self.catalog))
            elif self.path == "/api/import/bundle/save":
                self.json_response(
                    save_review_bundle(self.body(), self.catalog, self.store),
                    HTTPStatus.CREATED,
                )
            elif self.path == "/api/mealie/sync/verify":
                if not self.mealie_sync:
                    self.json_response({"error": "Mealie確認はHome Assistant環境で利用できます"}, 409)
                    return
                data = self.body()
                recipe_id = str(data.get("recipe_id") or "")
                recipe = self.store.get(recipe_id)
                sync_state = self.store.mealie_sync_state(recipe_id)
                if not recipe or not sync_state or sync_state.get("status") != "outcome_unknown":
                    self.json_response({"error": "確認する同期結果が見つかりません"}, 404)
                    return
                mealie_slug = str(sync_state.get("mealie_slug") or "")
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", mealie_slug):
                    self.json_response({"error": "確認するMealieレシピが特定できません"}, 409)
                    return
                try:
                    remote = self.catalog.get(mealie_slug)
                except IntegrationError:
                    self.json_response({"error": "Mealieから確認結果を取得できませんでした"}, 502)
                    return
                if not remote or remote.get("source_kind") != "mealie":
                    self.json_response({"error": "Mealie側のレシピを確認できませんでした"}, 409)
                    return
                matched = ingredient_amounts_match(recipe, remote)
                self.store.resolve_mealie_sync_verification(recipe_id, mealie_slug, matched)
                self.json_response(
                    {
                        "status": "synced" if matched else "retry_ready",
                        "mealie_slug": mealie_slug,
                    }
                )
            elif self.path == "/api/mealie/sync":
                if not self.mealie_sync:
                    self.json_response({"error": "Mealie同期はHome Assistant環境で利用できます"}, 409)
                    return
                data = self.body()
                recipe_id = str(data.get("recipe_id") or "")
                recipe = self.store.get(recipe_id)
                if not recipe:
                    self.json_response({"error": "同期する下書きが見つかりません"}, 404)
                    return
                if not recipe["servings_confirmed"]:
                    self.json_response(
                        {"error": "基準人数を確認してからMealieへ同期してください"},
                        409,
                    )
                    return
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {key: recipe.get(key) for key in (
                            "title", "description", "servings", "source_url", "video_url",
                            "portion_kind", "serving_volume_ml",
                            "dilution_concentrate_parts", "dilution_mixer_parts",
                            "meat_g_per_person", "rice_go_per_person",
                            "ingredients", "steps", "step_video_timestamps", "tags",
                        )},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                sync_state, slug, previous = self.store.begin_mealie_sync(recipe_id, fingerprint)
                if sync_state == "synced":
                    self.json_response({**(previous or {}), "replayed": True})
                    return
                if sync_state in {"pending", "outcome_unknown"}:
                    self.json_response(
                        {
                            "error": "前回の同期結果をMealie側で確認してください",
                            "status": sync_state,
                            "mealie_slug": slug,
                        },
                        409,
                    )
                    return
                try:
                    if not slug:
                        slug = self.mealie_sync.create(recipe)
                        self.store.set_mealie_slug(recipe_id, slug)
                    self.mealie_sync.update(slug, recipe)
                except Exception:
                    self.store.mark_mealie_sync_unknown(recipe_id)
                    self.json_response(
                        {
                            "error": (
                                "同期結果を確認できないため自動再送を停止しました。"
                                "Mealie側を確認してください"
                            ),
                            "status": "outcome_unknown",
                            "mealie_slug": slug,
                        },
                        502,
                    )
                    return
                result = {"status": "synced", "mealie_slug": slug}
                self.store.finish_mealie_sync(recipe_id, result)
                self.json_response(result)
            elif self.path == "/api/timer/action":
                data = self.body()
                request_id = str(data.get("request_id", ""))
                try:
                    if str(uuid.UUID(request_id)) != request_id:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("request IDが正しくありません") from exc
                operation = str(data.get("operation", ""))
                try:
                    minutes = int(data.get("minutes", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("タイマー時間が正しくありません") from exc
                if operation not in {"start", "stop"}:
                    raise ValueError("タイマー操作が正しくありません")
                if operation == "start" and not 1 <= minutes <= 180:
                    raise ValueError("タイマーは1分から180分で指定してください")
                if operation == "stop":
                    minutes = 0
                fingerprint = hashlib.sha256(
                    json.dumps(
                        ["timer", operation, minutes],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                action_state, previous = self.store.begin_action(request_id, fingerprint)
                if action_state == "completed":
                    self.json_response({**(previous or {}), "replayed": True})
                    return
                if action_state in {"pending", "outcome_unknown"}:
                    self.json_response(
                        {
                            "error": "前回のタイマー操作結果をHome Assistant側で確認してください",
                            "request_id": request_id,
                            "status": action_state,
                        },
                        409,
                    )
                    return
                try:
                    result = self.timer.perform(operation, minutes)
                except Exception:
                    self.store.mark_action_unknown(request_id)
                    self.json_response(
                        {
                            "error": "タイマー操作結果を確認できないため自動再送を停止しました",
                            "request_id": request_id,
                            "status": "outcome_unknown",
                        },
                        502,
                    )
                    return
                result["request_id"] = request_id
                result["status"] = "completed"
                self.store.finish_action(request_id, result)
                self.json_response(result)
            elif self.path == "/api/lists/add":
                data = self.body()
                request_id = str(data.get("request_id", ""))
                try:
                    if str(uuid.UUID(request_id)) != request_id:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("request IDが正しくありません") from exc
                kind = str(data.get("kind", ""))
                enabled = (
                    self.runtime.public_status().get("shopping_list_enabled")
                    if kind == "shopping"
                    else self.runtime.public_status().get("task_list_enabled")
                    if kind == "tasks"
                    else False
                )
                if not enabled:
                    label = "買い物リスト" if kind == "shopping" else "タスクリスト"
                    self.json_response(
                        {"error": f"{label}のtodo entity IDをApp設定で指定してください"},
                        409,
                    )
                    return
                recipe = self.catalog.get(str(data.get("recipe_id", "")))
                if not recipe:
                    self.json_response({"error": "レシピが見つかりません"}, 404)
                    return
                try:
                    multiplier = float(data.get("multiplier", 1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("倍率が正しくありません") from exc
                items = build_recipe_list_items(recipe, kind, multiplier)
                fingerprint = hashlib.sha256(
                    json.dumps(
                        ["list", recipe["id"], kind, multiplier, items],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                action_state, previous = self.store.begin_action(request_id, fingerprint)
                if action_state == "completed":
                    self.json_response({**(previous or {}), "replayed": True})
                    return
                if action_state in {"pending", "outcome_unknown"}:
                    self.json_response(
                        {
                            "error": "前回のリスト追加結果をHome Assistant側で確認してください",
                            "request_id": request_id,
                            "status": action_state,
                        },
                        409,
                    )
                    return
                try:
                    result = self.todo.add(kind, items)
                except Exception:
                    self.store.mark_action_unknown(request_id)
                    self.json_response(
                        {
                            "error": "リスト追加結果を確認できないため自動再送を停止しました",
                            "request_id": request_id,
                            "status": "outcome_unknown",
                        },
                        502,
                    )
                    return
                result.update(
                    request_id=request_id,
                    recipe_id=str(recipe["id"]),
                    status="completed",
                )
                self.store.finish_action(request_id, result)
                self.json_response(result)
            elif self.path == "/api/cooking/action":
                data = self.body()
                request_id = str(data.get("request_id", ""))
                try:
                    if str(uuid.UUID(request_id)) != request_id:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("request IDが正しくありません") from exc
                recipe = self.catalog.get(str(data.get("recipe_id", "")))
                if not recipe:
                    self.json_response({"error": "レシピが見つかりません"}, 404)
                    return
                try:
                    step_number = int(data.get("step_number", 1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("工程番号が正しくありません") from exc
                operation = str(data.get("operation", "show"))
                speak = bool(data.get("speak", False))
                if operation not in {"start", "show", "next", "previous"} or not recipe.get("steps"):
                    raise ValueError("工程操作が正しくありません")
                fingerprint = hashlib.sha256(
                    json.dumps(
                        [recipe["id"], operation, step_number, speak],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                action_state, previous = self.store.begin_action(request_id, fingerprint)
                if action_state == "completed":
                    self.json_response({**(previous or {}), "replayed": True})
                    return
                if action_state in {"pending", "outcome_unknown"}:
                    self.json_response(
                        {
                            "error": "前回の操作結果をHome Assistant側で確認してください",
                            "request_id": request_id,
                            "status": action_state,
                        },
                        409,
                    )
                    return
                try:
                    result = self.cooking.perform(recipe, operation, step_number, speak)
                except Exception:
                    self.store.mark_action_unknown(request_id)
                    self.json_response(
                        {
                            "error": "操作結果を確認できないため自動再送を停止しました",
                            "request_id": request_id,
                            "status": "outcome_unknown",
                        },
                        502,
                    )
                    return
                result["request_id"] = request_id
                result["status"] = "completed"
                self.store.finish_action(request_id, result)
                self.json_response(result)
            else:
                self.json_response({"error": "見つかりません"}, 404)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception:
            self.json_response({"error": "処理中に問題が発生しました"}, 500)

    def do_PUT(self) -> None:
        if not self.ingress_allowed():
            return
        match = re.fullmatch(r"/api/recipes/([0-9a-f-]+)", self.path)
        if not match:
            self.json_response({"error": "見つかりません"}, 404)
            return
        try:
            if not self.store.get(match.group(1)):
                self.json_response({"error": "レシピが見つかりません"}, 404)
                return
            self.json_response(self.store.save(self.body(), match.group(1)))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)

    def do_DELETE(self) -> None:
        if not self.ingress_allowed():
            return
        match = re.fullmatch(r"/api/recipes/([0-9a-f-]+)", self.path)
        if not match:
            self.json_response({"error": "見つかりません"}, 404)
            return
        removed = self.store.delete(match.group(1))
        self.json_response({"deleted": removed}, 200 if removed else 404)

    def serve_static(self, url_path: str) -> None:
        relative = "index.html" if url_path in {"", "/"} else unquote(url_path).lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self.send_error(404)
            return
        if not target.is_file():
            target = STATIC_DIR / "index.html"
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        content_security_policy = (
            "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
            "script-src 'self'; media-src https:; "
            "frame-src https://www.youtube.com https://www.youtube-nocookie.com "
            "https://player.vimeo.com"
        )
        self.send_header("Content-Security-Policy", content_security_policy)
        self.end_headers()
        self.wfile.write(payload)


def make_server(host: str, port: int, db_path: Path) -> ThreadingHTTPServer:
    runtime = RuntimeConfig.from_environment()
    store = RecipeStore(db_path)
    if runtime.mode in {"stage", "prototype"}:
        recipe_reader: RecipeReader = MockMealieCatalog(MOCK_MEALIE_FIXTURE)
        cooking: StageCookingController | HomeAssistantCookingClient = StageCookingController()
        if runtime.mode == "prototype":
            timer: StageTimerController | HomeAssistantTimerClient = HomeAssistantTimerClient(
                UrllibJsonTransport(
                    "http://supervisor/core",
                    os.environ.get("SUPERVISOR_TOKEN", ""),
                ),
                runtime.timer_script_entity_id,
            )
        else:
            timer = StageTimerController()
        todo: StageTodoController | HomeAssistantTodoClient = StageTodoController()
        images = None
        mealie_sync = None
    else:
        config_path = Path(os.environ.get("COOKING_ASSISTANT_EXTERNAL_CONFIG", "/config/integrations.json"))
        external = ExternalIntegrationConfig.load(config_path)
        mealie_transport = UrllibJsonTransport(external.mealie_url, external.mealie_token)
        recipe_reader = MealieReadClient(mealie_transport)
        mealie_sync = MealieSyncClient(mealie_transport)
        cooking = HomeAssistantCookingClient(
            UrllibJsonTransport(external.home_assistant_url, os.environ.get("SUPERVISOR_TOKEN", "")),
            external.script_entity_id,
        )
        timer = HomeAssistantTimerClient(
            UrllibJsonTransport(external.home_assistant_url, os.environ.get("SUPERVISOR_TOKEN", "")),
            external.timer_script_entity_id,
        )
        todo = HomeAssistantTodoClient(
            UrllibJsonTransport(external.home_assistant_url, os.environ.get("SUPERVISOR_TOKEN", "")),
            runtime.shopping_list_entity_id,
            runtime.task_list_entity_id,
        )
        images = MealieImageClient(
            UrllibJsonTransport(external.mealie_url, external.mealie_token)
        )
    catalog = CombinedRecipeCatalog(recipe_reader, store)
    handler = type(
        "KitchenHandler",
        (AppHandler,),
        {
            "store": store,
            "catalog": catalog,
            "runtime": runtime,
            "cooking": cooking,
            "timer": timer,
            "todo": todo,
            "images": images,
            "mealie_sync": mealie_sync,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kitchen Companion server")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8099")))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"Kitchen Companion listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

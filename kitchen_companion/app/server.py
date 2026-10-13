from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sqlite3
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
    IntegrationError,
    MealieImageClient,
    MealieReadClient,
    MealieSyncClient,
    UrllibJsonTransport,
)
from .integrations import RuntimeConfig, StageCookingController, StageTimerController

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MOCK_MEALIE_FIXTURE = APP_DIR / "fixtures" / "mock_mealie.json"
DEFAULT_DB = Path(os.environ.get("COOKING_ASSISTANT_DB", "/data/recipes.sqlite3"))
MAX_BODY = 1_000_000
MAX_IMPORT = 3_000_000


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


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
        result["source_kind"] = "draft"
        result["read_only"] = False
        return result

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
                   (id, title, description, servings, source_url, video_url, image_url,
                    portion_kind, serving_volume_ml, dilution_concentrate_parts,
                    dilution_mixer_parts, meat_g_per_person, rice_go_per_person,
                    ingredients_json, steps_json, step_video_timestamps_json,
                    tags_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    servings=excluded.servings, source_url=excluded.source_url,
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


def parse_ingredient(text: str) -> dict[str, Any]:
    original_text = str(text).strip()[:500]
    text = original_text.replace("½", "1/2").replace("¼", "1/4").replace("¾", "3/4")
    number_pattern = r"(\d+(?:\.\d+)?|\d+\s*/\s*\d+)"
    unit_pattern = r"(大さじ|小さじ|カップ|ml|mL|l|L|g|kg|個|本|枚|片|株|袋|缶|束|合)"
    match = re.match(rf"^\s*{number_pattern}\s*{unit_pattern}?\s+(.+)$", text)
    if match:
        number, unit, name = match.group(1), match.group(2) or "", match.group(3)
    else:
        match = re.match(rf"^(.+?)\s+{unit_pattern}\s*{number_pattern}\s*$", text)
        if match:
            name, unit, number = match.group(1), match.group(2), match.group(3)
        else:
            match = re.match(rf"^(.+?)\s+{number_pattern}\s*{unit_pattern}\s*$", text)
            if not match:
                return {
                    "amount": None,
                    "amount_max": None,
                    "amount_text": "",
                    "unit": "",
                    "name": text[:200],
                    "note": "",
                    "original_text": original_text,
                }
            name, number, unit = match.group(1), match.group(2), match.group(3)
    number = number.replace(" ", "")
    if "/" in number:
        numerator, denominator = number.split("/", 1)
        amount = float(numerator) / float(denominator)
    else:
        amount = float(number)
    return {
        "amount": amount,
        "amount_max": None,
        "amount_text": number[:50],
        "unit": unit,
        "name": name[:200],
        "note": "",
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
    servings_match = re.search(r"(\d+(?:\.\d+)?)\s*人分", description)
    servings = float(servings_match.group(1)) if servings_match else 2
    ingredients: list[dict[str, Any]] = []
    steps: list[str] = []
    section = ""
    ingredient_heading = re.compile(r"^(?:[#【\[（(\s]*)(?:材料|ingredients?)", re.IGNORECASE)
    step_heading = re.compile(
        r"^(?:[#【\[（(\s]*)(?:作り方|手順|調理手順|directions?|instructions?|method)",
        re.IGNORECASE,
    )
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if ingredient_heading.match(line):
            section = "ingredients"
            continue
        if step_heading.match(line):
            section = "steps"
            continue
        if not line or not section:
            continue
        if line.startswith(("http://", "https://")) or (line.startswith("#") and len(line) < 80):
            section = ""
            continue
        cleaned = re.sub(r"^[\s・●◯○■□✓✔*-]+", "", line)
        if section == "steps":
            cleaned = re.sub(r"^\d+[.)．、:](?!\d)\s*", "", cleaned)
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


def recipe_draft_from_node(node: dict[str, Any], source_url: str, video_url: str = "") -> dict[str, Any]:
    image = node.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    yield_value = node.get("recipeYield", 2)
    if isinstance(yield_value, list):
        yield_value = yield_value[0] if yield_value else 2
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
            "source_url": source_url,
            "video_url": video_url or extract_video_url(node.get("video")),
            "image_url": urljoin(source_url, str(image)) if image else "",
            "ingredients": [parse_ingredient(item) for item in node.get("recipeIngredient", [])],
            "steps": steps,
            "step_video_timestamps": step_video_timestamps,
            "tags": ["動画"] if video_url else [],
        }
    )


def video_draft_from_html(url: str, html: str) -> dict[str, Any]:
    structured = parse_json_ld_nodes(html)
    recipe_node = next((node for value in structured if (node := find_recipe_node(value))), None)
    if recipe_node:
        return recipe_draft_from_node(recipe_node, url, url)

    video_node = next(
        (node for value in structured if (node := find_typed_node(value, "VideoObject"))),
        None,
    )
    metadata = PageMetadataParser()
    metadata.feed(html)
    title = str((video_node or {}).get("name") or "").strip()
    if not title:
        title = metadata.metadata.get("og:title") or metadata.metadata.get("twitter:title") or metadata.title
    description = str((video_node or {}).get("description") or "").strip()
    if not description:
        description = (
            metadata.metadata.get("og:description")
            or metadata.metadata.get("twitter:description")
            or metadata.metadata.get("description")
            or ""
        )
    image = (video_node or {}).get("thumbnailUrl", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if not image:
        image = metadata.metadata.get("og:image") or metadata.metadata.get("twitter:image") or ""
    servings, ingredients, steps = extract_description_recipe(description)
    steps, step_video_timestamps = steps_with_video_timestamps(steps)
    try:
        image_url = safe_image_url(urljoin(url, str(image or "")))
    except ValueError:
        image_url = ""
    return validate_recipe(
        {
            "title": title or "動画レシピ",
            "description": description,
            "servings": servings,
            "source_url": url,
            "video_url": url,
            "image_url": image_url,
            "ingredients": ingredients,
            "steps": steps,
            "step_video_timestamps": step_video_timestamps,
            "tags": ["動画"],
        }
    )


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

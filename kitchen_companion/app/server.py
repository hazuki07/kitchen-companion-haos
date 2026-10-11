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
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import CombinedRecipeCatalog, MockMealieCatalog, RecipeReader
from .clients import (
    ExternalIntegrationConfig,
    HomeAssistantCookingClient,
    HomeAssistantTimerClient,
    IntegrationError,
    MealieImageClient,
    MealieReadClient,
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
                    ingredients_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
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
                """
            )
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
        for key in ("ingredients_json", "steps_json", "tags_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["source_kind"] = "draft"
        result["read_only"] = False
        return result

    def list(self, query: str = "") -> list[dict[str, Any]]:
        with self.connect() as db:
            if query:
                token = f"%{query.strip()}%"
                rows = db.execute(
                    """SELECT * FROM recipes
                       WHERE title LIKE ? OR description LIKE ? OR tags_json LIKE ?
                       ORDER BY updated_at DESC""",
                    (token, token, token),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM recipes ORDER BY updated_at DESC").fetchall()
        return [self._row(row) for row in rows]

    def get(self, recipe_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
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
                    ingredients_json, steps_json, tags_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    servings=excluded.servings, source_url=excluded.source_url,
                    video_url=excluded.video_url, image_url=excluded.image_url,
                    ingredients_json=excluded.ingredients_json,
                    steps_json=excluded.steps_json, tags_json=excluded.tags_json,
                    updated_at=excluded.updated_at""",
                (
                    recipe_id,
                    recipe["title"],
                    recipe["description"],
                    recipe["servings"],
                    recipe["source_url"],
                    recipe["video_url"],
                    recipe["image_url"],
                    json.dumps(recipe["ingredients"], ensure_ascii=False),
                    json.dumps(recipe["steps"], ensure_ascii=False),
                    json.dumps(recipe["tags"], ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
        return self.get(recipe_id)  # type: ignore[return-value]

    def delete(self, recipe_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        return cursor.rowcount > 0

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
    ingredients = raw.get("ingredients", [])
    steps = raw.get("steps", [])
    tags = raw.get("tags", [])
    if not isinstance(ingredients, list) or not isinstance(steps, list) or not isinstance(tags, list):
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
            clean_ingredients.append(
                {
                    "amount": amount,
                    "unit": str(item.get("unit", ""))[:30],
                    "name": str(item["name"]).strip()[:200],
                    "note": str(item.get("note", ""))[:200],
                }
            )
    clean_steps = [str(step).strip()[:2000] for step in steps[:100] if str(step).strip()]
    return {
        "title": title,
        "description": str(raw.get("description", "")).strip()[:2000],
        "servings": servings,
        "source_url": safe_http_url(raw.get("source_url", ""), allow_empty=True),
        "video_url": safe_http_url(raw.get("video_url", ""), allow_empty=True),
        "image_url": safe_image_url(raw.get("image_url", "")),
        "ingredients": clean_ingredients,
        "steps": clean_steps,
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


def parse_ingredient(text: str) -> dict[str, Any]:
    text = str(text).strip().replace("½", "1/2").replace("¼", "1/4").replace("¾", "3/4")
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
                return {"amount": None, "unit": "", "name": text[:200], "note": ""}
            name, number, unit = match.group(1), match.group(2), match.group(3)
    number = number.replace(" ", "")
    if "/" in number:
        numerator, denominator = number.split("/", 1)
        amount = float(numerator) / float(denominator)
    else:
        amount = float(number)
    return {"amount": amount, "unit": unit, "name": name[:200], "note": ""}


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


def find_recipe_node(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        kinds = value.get("@type", [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if "Recipe" in kinds:
            return value
        for nested in value.values():
            found = find_recipe_node(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = find_recipe_node(nested)
            if found:
                return found
    return None


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


def import_recipe(url: str) -> dict[str, Any]:
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
        raise ValueError("レシピサイトを取得できませんでした") from exc
    parser = JsonLdParser()
    parser.feed(html)
    node = None
    for block in parser.blocks:
        try:
            node = find_recipe_node(json.loads(block))
        except json.JSONDecodeError:
            continue
        if node:
            break
    if not node:
        raise ValueError("このページからレシピ情報を見つけられませんでした")
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
    return validate_recipe(
        {
            "title": node.get("name", "取り込んだレシピ"),
            "description": node.get("description", ""),
            "servings": servings,
            "source_url": url,
            "image_url": image,
            "ingredients": [parse_ingredient(item) for item in node.get("recipeIngredient", [])],
            "steps": flatten_steps(node.get("recipeInstructions", [])),
            "tags": [],
        }
    )


class AppHandler(BaseHTTPRequestHandler):
    store: RecipeStore
    catalog: RecipeReader
    runtime: RuntimeConfig
    cooking: StageCookingController | HomeAssistantCookingClient
    timer: StageTimerController | HomeAssistantTimerClient
    images: MealieImageClient | None

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
            self.json_response({"recipes": self.catalog.list(query)})
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
            if self.path == "/api/recipes":
                self.json_response(self.store.save(self.body()), HTTPStatus.CREATED)
            elif self.path == "/api/import":
                data = self.body()
                self.json_response(import_recipe(str(data.get("url", ""))))
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
            "frame-src https://www.youtube.com https://www.youtube-nocookie.com"
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
    else:
        config_path = Path(os.environ.get("COOKING_ASSISTANT_EXTERNAL_CONFIG", "/config/integrations.json"))
        external = ExternalIntegrationConfig.load(config_path)
        recipe_reader = MealieReadClient(UrllibJsonTransport(external.mealie_url, external.mealie_token))
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

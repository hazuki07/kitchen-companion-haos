from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class IntegrationError(RuntimeError):
    pass


class RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        return None


@dataclass(frozen=True)
class ExternalIntegrationConfig:
    mealie_url: str
    mealie_token: str = field(repr=False)
    script_entity_id: str
    timer_script_entity_id: str
    home_assistant_url: str = "http://supervisor/core"

    @classmethod
    def load(cls, path: Path) -> ExternalIntegrationConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("外部接続設定を読み込めません") from exc
        if not isinstance(raw, dict):
            raise ValueError("外部接続設定の形式が正しくありません")
        mealie = raw.get("mealie") if isinstance(raw.get("mealie"), dict) else {}
        home_assistant = (
            raw.get("home_assistant") if isinstance(raw.get("home_assistant"), dict) else {}
        )
        config = cls(
            mealie_url=str(mealie.get("url") or ""),
            mealie_token=str(mealie.get("token") or ""),
            script_entity_id=str(home_assistant.get("script_entity_id") or ""),
            timer_script_entity_id=str(home_assistant.get("timer_script_entity_id") or ""),
        )
        UrllibJsonTransport(config.mealie_url, config.mealie_token)
        home_assistant_transport = UrllibJsonTransport(
            config.home_assistant_url,
            os.environ.get("SUPERVISOR_TOKEN", ""),
        )
        HomeAssistantCookingClient(
            home_assistant_transport,
            config.script_entity_id,
        )
        HomeAssistantTimerClient(home_assistant_transport, config.timer_script_entity_id)
        return config


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any: ...


class BinaryTransport(Protocol):
    def request_bytes(self, path: str, *, max_bytes: int) -> tuple[str, bytes]: ...


@dataclass(frozen=True)
class UrllibJsonTransport:
    base_url: str
    token: str = field(repr=False)
    timeout: float = 10
    max_response_bytes: int = 5_000_000
    auth_header_name: str = "Authorization"
    auth_prefix: str = "Bearer "

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("接続先URLが正しくありません")
        if not self.token:
            raise ValueError("接続tokenが設定されていません")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.auth_header_name):
            raise ValueError("認証header名が正しくありません")
        if not 1 <= self.max_response_bytes <= 10_000_000:
            raise ValueError("JSON応答サイズ上限が正しくありません")

    def _auth_header(self) -> dict[str, str]:
        return {self.auth_header_name: f"{self.auth_prefix}{self.token}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API pathが正しくありません")
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = Request(
            url,
            data=payload,
            method=method,
            headers={
                **self._auth_header(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with build_opener(RejectRedirectHandler()).open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise IntegrationError("外部サービスとの通信に失敗しました") from exc
        if len(raw) > self.max_response_bytes:
            raise IntegrationError("外部サービスの応答が大きすぎます")
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise IntegrationError("外部サービスから不正な応答を受け取りました") from exc

    def request_bytes(self, path: str, *, max_bytes: int) -> tuple[str, bytes]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API pathが正しくありません")
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            method="GET",
            headers={**self._auth_header(), "Accept": "image/*"},
        )
        try:
            with build_opener(RejectRedirectHandler()).open(request, timeout=self.timeout) as response:
                content_type = response.headers.get_content_type()
                payload = response.read(max_bytes + 1)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise IntegrationError("画像サービスとの通信に失敗しました") from exc
        if len(payload) > max_bytes:
            raise IntegrationError("画像が大きすぎます")
        return content_type, payload


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        return float(match.group()) if match else None
    return None


def _timestamp_list(value: Any, length: int) -> list[int | None]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, list):
        value = []
    result: list[int | None] = []
    for item in value[:length]:
        number = _number(item)
        result.append(int(number) if number is not None and 0 <= number <= 86_400 else None)
    return result + [None] * (length - len(result))


_INGREDIENT_NUMBER = r"(?:\d+\s*/\s*\d+|\d+(?:\.\d+)?)"
_INGREDIENT_RANGE = rf"{_INGREDIENT_NUMBER}(?:\s*[〜～~-]\s*{_INGREDIENT_NUMBER})?"
_INGREDIENT_UNIT = (
    r"ml|mL|L|l|kg|g|個|本|枚|片|株|袋|缶|束|合|杯|玉|丁|粒|房|尾|切れ|パック"
)
_INGREDIENT_PREFIX_UNIT = r"ティースプーン|大さじ|小さじ|カップ"
_LEGACY_AMOUNT = (
    rf"(?:{_INGREDIENT_PREFIX_UNIT})\s*{_INGREDIENT_RANGE}(?:杯分)?"
    rf"|{_INGREDIENT_RANGE}\s*(?:{_INGREDIENT_UNIT})(?:分)?"
    r"|適量|(?:多めの)?少々|ひとつまみ"
)
_LEGACY_INGREDIENT_PAIR = re.compile(
    rf"(?P<name>.+?)\s*(?:\.{{2,}}|…+)\s*(?P<amount>{_LEGACY_AMOUNT})"
    rf"(?=\s+.+?\s*(?:\.{{2,}}|…+)\s*(?:{_LEGACY_AMOUNT})|$)"
)


def _legacy_amount(value: str) -> tuple[float | None, float | None, str, str, str]:
    text = value.strip()
    if text in {"適量", "少々", "多めの少々", "ひとつまみ"}:
        return None, None, "", "", text
    prefix = re.fullmatch(
        rf"(?P<unit>{_INGREDIENT_PREFIX_UNIT})\s*(?P<amount>{_INGREDIENT_RANGE})(?:杯分)?",
        text,
    )
    suffix = re.fullmatch(
        rf"(?P<amount>{_INGREDIENT_RANGE})\s*(?P<unit>{_INGREDIENT_UNIT})(?:分)?",
        text,
    )
    match = prefix or suffix
    if not match:
        return None, None, "", "", text
    amount_text = re.sub(r"\s+", "", match.group("amount")).replace("～", "〜")
    bounds = re.split(r"[〜~-]", amount_text, maxsplit=1)

    def number(part: str) -> float | None:
        if "/" not in part:
            return float(part)
        numerator, denominator = part.split("/", 1)
        if float(denominator) == 0:
            return None
        return float(numerator) / float(denominator)

    amount = number(bounds[0])
    amount_max = number(bounds[1]) if len(bounds) == 2 else None
    if amount is None or (len(bounds) == 2 and amount_max is None):
        return None, None, "", "", text
    return amount, amount_max, amount_text, match.group("unit"), ""


def _legacy_ingredients(text: str) -> list[dict[str, Any]]:
    text = re.sub(r"\s+", " ", text).strip()
    if not re.search(r"(?:\.{2,}|…+)", text):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in _LEGACY_INGREDIENT_PAIR.finditer(text):
        name = match.group("name").strip()
        amount_source = match.group("amount").strip()
        key = (name, amount_source)
        if not name or key in seen:
            continue
        seen.add(key)
        amount, amount_max, amount_text, unit, note = _legacy_amount(amount_source)
        original_text = f"{name}...{amount_source}"
        result.append(
            {
                "amount": amount,
                "amount_max": amount_max,
                "amount_text": amount_text,
                "unit": unit[:30],
                "name": name[:200],
                "note": note,
                "original_text": original_text[:500],
            }
        )
    return result


def _simple_ingredient(text: str) -> dict[str, Any] | None:
    text = re.sub(r"\s+", " ", text).strip()
    match = re.fullmatch(
        rf"(?P<name>.+)(?:\s+|[:：])(?P<amount>{_LEGACY_AMOUNT})",
        text,
    )
    if not match:
        return None
    name = match.group("name").strip()
    amount_source = match.group("amount").strip()
    amount, amount_max, amount_text, unit, note = _legacy_amount(amount_source)
    return {
        "amount": amount,
        "amount_max": amount_max,
        "amount_text": amount_text,
        "unit": unit[:30],
        "name": name[:200],
        "note": note,
        "original_text": text[:500],
    }


def _ingredient(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        return {
            "amount": None,
            "amount_max": None,
            "amount_text": "",
            "unit": "",
            "name": raw[:200],
            "note": "未確認",
            "original_text": raw[:500],
        }
    if not isinstance(raw, dict):
        return None
    food = raw.get("food") if isinstance(raw.get("food"), dict) else {}
    unit = raw.get("unit") if isinstance(raw.get("unit"), dict) else {}
    display = str(raw.get("display") or raw.get("originalText") or "").strip()
    name = str(food.get("name") or raw.get("note") or display or "材料名未確認").strip()
    amount = _number(raw.get("quantity"))
    if amount is not None and amount <= 0:
        amount = None
    amount_text = (
        str(raw.get("quantity") if raw.get("quantity") is not None else "").strip()
        if amount is not None
        else ""
    )
    note = str(raw.get("note") or "").strip()
    original_text = str(raw.get("originalText") or "").strip()
    if note and note in {name, display, original_text}:
        note = ""
    return {
        "amount": amount,
        "amount_max": None,
        "amount_text": amount_text[:50],
        "unit": str(unit.get("abbreviation") or unit.get("name") or "")[:30],
        "name": name[:200],
        "note": note[:200],
        "original_text": (original_text or display or name)[:500],
    }


def _ingredients(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for value in raw:
        if isinstance(value, dict):
            food = value.get("food") if isinstance(value.get("food"), dict) else {}
            candidates = (
                value.get("display"),
                value.get("originalText"),
                food.get("name"),
                value.get("note"),
            )
            for candidate in candidates:
                normalized = _legacy_ingredients(str(candidate or ""))
                if normalized:
                    result.extend(normalized)
                    break
            else:
                quantity = _number(value.get("quantity"))
                if quantity is None or quantity <= 0:
                    simple_candidates = (
                        food.get("name"),
                        value.get("originalText"),
                        value.get("display"),
                        value.get("note"),
                    )
                    simple = next(
                        (
                            item
                            for candidate in simple_candidates
                            if (item := _simple_ingredient(str(candidate or "")))
                        ),
                        None,
                    )
                    if simple:
                        result.append(simple)
                        continue
                if fallback := _ingredient(value):
                    result.append(fallback)
        elif item := _ingredient(value):
            result.append(item)
    return result[:200]


def _instructions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        text = item.get("text", "") if isinstance(item, dict) else item
        if str(text).strip():
            result.append(str(text).strip()[:2000])
    return result


def _http_url(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            if url := _http_url(item):
                return url
        return ""
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "embedUrl", "value"):
            if url := _http_url(value.get(key)):
                return url
        return ""
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
        return candidate[:2000]
    return ""


def _video_url(raw: dict[str, Any]) -> str:
    for key in (
        "videoURL", "videoUrl", "video", "recipeVideo", "youtubeURL", "youtubeUrl"
    ):
        if url := _http_url(raw.get(key)):
            return url
    extras = raw.get("extras") if isinstance(raw.get("extras"), dict) else {}
    for key, value in extras.items():
        normalized = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠]+", "", str(key).casefold())
        if normalized in {
            "video", "videourl", "recipevideo", "youtube", "youtubeurl", "動画", "動画url"
        }:
            if url := _http_url(value):
                return url
    source_url = _http_url(raw.get("orgURL"))
    hostname = (urlparse(source_url).hostname or "").casefold().removeprefix("www.")
    hostname = hostname.removeprefix("m.")
    if hostname in {"youtu.be", "youtube.com", "youtube-nocookie.com", "vimeo.com"}:
        return source_url
    return ""


class MealieReadClient:
    def __init__(self, transport: JsonTransport):
        self.transport = transport

    def list(self, query: str = "") -> list[dict[str, Any]]:
        recipes: list[dict[str, Any]] = []
        seen_slugs: set[str] = set()
        per_page = 50
        for page in range(1, 21):
            response = self.transport.request(
                "GET",
                "/api/recipes",
                query={
                    "search": query[:160],
                    "page": page,
                    "perPage": per_page,
                    "orderBy": "name",
                    "orderDirection": "asc",
                },
            )
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise IntegrationError("Mealieの一覧応答が正しくありません")
            items = response["items"]
            new_slugs: list[str] = []
            for summary in items:
                if not isinstance(summary, dict) or not summary.get("slug"):
                    continue
                slug = str(summary["slug"])
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                new_slugs.append(slug)
            recipes.extend(
                recipe
                for slug in new_slugs
                if (recipe := self.get(slug)) is not None
            )

            total = response.get("total")
            total_pages = response.get("total_pages", response.get("pages"))
            try:
                total_number = int(total) if total is not None else None
                page_count = int(total_pages) if total_pages is not None else None
            except (TypeError, ValueError):
                total_number = page_count = None
            if total_number is not None and len(seen_slugs) >= total_number:
                break
            if page_count is not None and page >= page_count:
                break
            if "next" in response:
                if not response.get("next"):
                    break
            elif len(items) < per_page or not new_slugs:
                break
        return [recipe for recipe in recipes if recipe is not None]

    def get(self, recipe_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", recipe_id):
            raise ValueError("Mealie recipe slugが正しくありません")
        raw = self.transport.request("GET", f"/api/recipes/{quote(recipe_id, safe='')}")
        if not isinstance(raw, dict):
            raise IntegrationError("Mealieの詳細応答が正しくありません")
        ingredients = _ingredients(raw.get("recipeIngredient"))
        servings = _number(raw.get("recipeServings") or raw.get("recipeYield"))
        tags = raw.get("tags", []) if isinstance(raw.get("tags"), list) else []
        extras = raw.get("extras") if isinstance(raw.get("extras"), dict) else {}
        portion_kind = str(extras.get("portion_kind") or "standard")
        if portion_kind not in {
            "standard", "meat_main", "mixed_meat", "drink", "diluted_drink"
        }:
            portion_kind = "standard"
        image_id = str(raw.get("id") or "")
        image_url = f"api/catalog/images/{quote(image_id, safe='')}" if raw.get("image") and image_id else ""
        steps = _instructions(raw.get("recipeInstructions"))
        return {
            "id": str(raw.get("slug") or raw.get("id") or recipe_id),
            "title": str(raw.get("name") or "料理名未確認")[:160],
            "description": str(raw.get("description") or "")[:2000],
            "servings": servings or 1,
            "servings_confirmed": servings is not None and servings > 0,
            "source_url": _http_url(raw.get("orgURL")),
            "video_url": _video_url(raw),
            "image_url": image_url,
            "portion_kind": portion_kind,
            "serving_volume_ml": _number(extras.get("serving_volume_ml")) or 300,
            "dilution_concentrate_parts": (
                _number(extras.get("dilution_concentrate_parts")) or 3
            ),
            "dilution_mixer_parts": _number(extras.get("dilution_mixer_parts")) or 7,
            "meat_g_per_person": _number(extras.get("meat_g_per_person")) or 0,
            "rice_go_per_person": _number(extras.get("rice_go_per_person")) or 0,
            "ingredients": ingredients,
            "steps": steps,
            "step_video_timestamps": _timestamp_list(
                extras.get("step_video_timestamps"), len(steps)
            ),
            "tags": [
                str(tag.get("name") if isinstance(tag, dict) else tag)[:50]
                for tag in tags[:30]
                if str(tag.get("name") if isinstance(tag, dict) else tag).strip()
            ],
            "source_kind": "mealie",
            "read_only": True,
        }


class MealieSyncClient:
    """Explicitly publish local recipes to Mealie.

    Creation and update are separate so the caller can persist the returned slug
    before sending the recipe body. This prevents an uncertain PATCH result from
    causing a second recipe to be created automatically.
    """

    def __init__(self, transport: JsonTransport):
        self.transport = transport

    def create(self, recipe: dict[str, Any]) -> str:
        response = self.transport.request(
            "POST",
            "/api/recipes",
            body={"name": str(recipe.get("title") or "料理名未確認")[:160]},
        )
        slug = (
            response
            if isinstance(response, str)
            else response.get("slug") if isinstance(response, dict) else ""
        )
        if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            raise IntegrationError("Mealieの作成応答が正しくありません")
        return slug

    def update(self, slug: str, recipe: dict[str, Any]) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            raise ValueError("Mealie recipe slugが正しくありません")
        ingredients = []
        for item in recipe.get("ingredients", []):
            amount = item.get("amount")
            unit = str(item.get("unit") or "").strip()
            name = str(item.get("name") or "").strip()
            note = str(item.get("note") or "").strip()
            original_text = str(item.get("original_text") or "").strip()
            display = " ".join(
                part
                for part in (
                    f"{amount:g}" if isinstance(amount, (int, float)) else "",
                    unit,
                    name,
                    note,
                )
                if part
            )
            ingredients.append(
                {
                    "quantity": amount,
                    "unit": unit or None,
                    "food": name or None,
                    "note": note,
                    "originalText": original_text or display,
                }
            )
        body: dict[str, Any] = {
            "name": str(recipe.get("title") or "料理名未確認")[:160],
            "description": str(recipe.get("description") or "")[:2000],
            "recipeServings": recipe.get("servings"),
            "recipeYieldQuantity": recipe.get("servings"),
            "recipeYield": "人分",
            "orgURL": str(recipe.get("source_url") or ""),
            "recipeIngredient": ingredients,
            "recipeInstructions": [
                {"text": str(step)[:2000]} for step in recipe.get("steps", []) if str(step).strip()
            ],
            "tags": [
                {"name": str(tag)[:50]} for tag in recipe.get("tags", []) if str(tag).strip()
            ],
            "extras": {
                "video_url": str(recipe.get("video_url") or ""),
                "portion_kind": str(recipe.get("portion_kind") or "standard"),
                "serving_volume_ml": recipe.get("serving_volume_ml", 300),
                "dilution_concentrate_parts": recipe.get("dilution_concentrate_parts", 3),
                "dilution_mixer_parts": recipe.get("dilution_mixer_parts", 7),
                "meat_g_per_person": recipe.get("meat_g_per_person", 0),
                "rice_go_per_person": recipe.get("rice_go_per_person", 0),
                "step_video_timestamps": json.dumps(
                    recipe.get("step_video_timestamps", []), ensure_ascii=False
                ),
            },
        }
        self.transport.request(
            "PATCH",
            f"/api/recipes/{quote(slug, safe='')}",
            body=body,
        )


class MealieImageClient:
    ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    MAX_IMAGE_BYTES = 8_000_000

    def __init__(self, transport: BinaryTransport):
        self.transport = transport

    def get(self, recipe_id: str) -> tuple[str, bytes]:
        if not re.fullmatch(r"[a-fA-F0-9-]{8,64}", recipe_id):
            raise ValueError("Mealie recipe IDが正しくありません")
        content_type, payload = self.transport.request_bytes(
            f"/api/media/recipes/{quote(recipe_id, safe='')}/images/original.webp",
            max_bytes=self.MAX_IMAGE_BYTES,
        )
        if content_type not in self.ALLOWED_TYPES:
            raise IntegrationError("Mealieから画像以外の応答を受け取りました")
        return content_type, payload


class HomeAssistantCookingClient:
    OPERATIONS = {"start", "show", "next", "previous"}

    def __init__(self, transport: JsonTransport, script_entity_id: str):
        if not re.fullmatch(r"script\.[a-z0-9_]+", script_entity_id):
            raise ValueError("Home Assistant script entity IDが正しくありません")
        self.transport = transport
        self.service = script_entity_id.removeprefix("script.")

    def perform(
        self,
        recipe: dict[str, Any],
        operation: str,
        step_number: int,
        speak: bool = False,
    ) -> dict[str, Any]:
        if operation not in self.OPERATIONS:
            raise ValueError("工程操作が正しくありません")
        if step_number < 1:
            raise ValueError("工程番号が正しくありません")
        steps = recipe.get("steps", [])
        if not steps:
            raise ValueError("工程が登録されていません")
        self.transport.request(
            "POST",
            f"/api/services/script/{self.service}",
            body={
                "recipe_id": str(recipe["id"]),
                "step_number": step_number,
                "operation": operation,
                "speak": bool(speak),
            },
        )
        index = min(len(steps) - 1, step_number - 1)
        if operation == "start":
            index = 0
        elif operation == "next":
            index = min(len(steps) - 1, index + 1)
        elif operation == "previous":
            index = max(0, index - 1)
        return {
            "mode": "home_assistant",
            "simulated": False,
            "recipe_id": str(recipe["id"]),
            "operation": operation,
            "step_number": index + 1,
            "step_count": len(steps),
            "text": steps[index],
            "speak": bool(speak),
            "speech_output": "home_assistant",
        }


class HomeAssistantTimerClient:
    ACTIONS = {"start", "stop"}
    MAX_MINUTES = 180

    def __init__(self, transport: JsonTransport, script_entity_id: str):
        if not re.fullmatch(r"script\.[a-z0-9_]+", script_entity_id):
            raise ValueError("Home Assistant timer script entity IDが正しくありません")
        self.transport = transport
        self.service = script_entity_id.removeprefix("script.")

    def perform(self, action: str, minutes: int) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError("タイマー操作が正しくありません")
        if action == "start" and not 1 <= minutes <= self.MAX_MINUTES:
            raise ValueError("タイマーは1分から180分で指定してください")
        if action == "stop":
            minutes = 0
        self.transport.request(
            "POST",
            f"/api/services/script/{self.service}",
            body={"operation": action, "minutes": minutes},
        )
        return {
            "mode": "home_assistant",
            "simulated": False,
            "operation": action,
            "minutes": minutes,
            "alert_output": "alexa",
        }


class HomeAssistantTodoClient:
    KINDS = {"shopping", "tasks"}

    def __init__(
        self,
        transport: JsonTransport,
        shopping_list_entity_id: str = "",
        task_list_entity_id: str = "",
    ):
        targets = {
            "shopping": shopping_list_entity_id,
            "tasks": task_list_entity_id,
        }
        for entity_id in targets.values():
            if entity_id and not re.fullmatch(r"todo\.[a-z0-9_]+", entity_id):
                raise ValueError("Home Assistant todo entity IDが正しくありません")
        self.transport = transport
        self.targets = targets

    def add(self, kind: str, items: list[str]) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise ValueError("リスト種別が正しくありません")
        entity_id = self.targets[kind]
        if not entity_id:
            label = "買い物リスト" if kind == "shopping" else "タスクリスト"
            raise ValueError(f"{label}のtodo entity IDが設定されていません")
        if not items or len(items) > 200:
            raise ValueError("追加する項目数が正しくありません")
        for item in items:
            clean_item = str(item).strip()
            if not clean_item or len(clean_item) > 500:
                raise ValueError("リスト項目が正しくありません")
            self.transport.request(
                "POST",
                "/api/services/todo/add_item",
                body={"entity_id": entity_id, "item": clean_item},
            )
        return {
            "mode": "home_assistant",
            "simulated": False,
            "kind": kind,
            "added_count": len(items),
        }

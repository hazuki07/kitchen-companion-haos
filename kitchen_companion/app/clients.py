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

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("接続先URLが正しくありません")
        if not self.token:
            raise ValueError("接続tokenが設定されていません")

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
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with build_opener(RejectRedirectHandler()).open(request, timeout=self.timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise IntegrationError("外部サービスとの通信に失敗しました") from exc
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
            headers={"Authorization": f"Bearer {self.token}", "Accept": "image/*"},
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


def _ingredient(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        return {"amount": None, "unit": "", "name": raw[:200], "note": "未確認"}
    if not isinstance(raw, dict):
        return None
    food = raw.get("food") if isinstance(raw.get("food"), dict) else {}
    unit = raw.get("unit") if isinstance(raw.get("unit"), dict) else {}
    display = str(raw.get("display") or raw.get("originalText") or "").strip()
    name = str(food.get("name") or raw.get("note") or display or "材料名未確認").strip()
    amount = _number(raw.get("quantity"))
    note = str(raw.get("note") or "").strip()
    if amount is None and not note:
        note = display or "未確認"
    return {
        "amount": amount,
        "unit": str(unit.get("abbreviation") or unit.get("name") or "")[:30],
        "name": name[:200],
        "note": note[:200],
    }


def _instructions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        text = item.get("text", "") if isinstance(item, dict) else item
        if str(text).strip():
            result.append(str(text).strip()[:2000])
    return result


class MealieReadClient:
    def __init__(self, transport: JsonTransport):
        self.transport = transport

    def list(self, query: str = "") -> list[dict[str, Any]]:
        response = self.transport.request(
            "GET",
            "/api/recipes",
            query={
                "search": query[:160],
                "page": 1,
                "perPage": 50,
                "orderBy": "name",
                "orderDirection": "asc",
            },
        )
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise IntegrationError("Mealieの一覧応答が正しくありません")
        recipes = []
        for summary in response["items"]:
            if not isinstance(summary, dict) or not summary.get("slug"):
                continue
            recipes.append(self.get(str(summary["slug"])))
        return [recipe for recipe in recipes if recipe is not None]

    def get(self, recipe_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", recipe_id):
            raise ValueError("Mealie recipe slugが正しくありません")
        raw = self.transport.request("GET", f"/api/recipes/{quote(recipe_id, safe='')}")
        if not isinstance(raw, dict):
            raise IntegrationError("Mealieの詳細応答が正しくありません")
        ingredients = [item for value in raw.get("recipeIngredient", []) if (item := _ingredient(value))]
        servings = _number(raw.get("recipeServings") or raw.get("recipeYield"))
        tags = raw.get("tags", []) if isinstance(raw.get("tags"), list) else []
        image_id = str(raw.get("id") or "")
        image_url = f"api/catalog/images/{quote(image_id, safe='')}" if raw.get("image") and image_id else ""
        return {
            "id": str(raw.get("slug") or raw.get("id") or recipe_id),
            "title": str(raw.get("name") or "料理名未確認")[:160],
            "description": str(raw.get("description") or "")[:2000],
            "servings": servings or 1,
            "servings_confirmed": servings is not None and servings > 0,
            "source_url": str(raw.get("orgURL") or "")[:2000],
            "video_url": "",
            "image_url": image_url,
            "ingredients": ingredients,
            "steps": _instructions(raw.get("recipeInstructions")),
            "tags": [
                str(tag.get("name") if isinstance(tag, dict) else tag)[:50]
                for tag in tags[:30]
                if str(tag.get("name") if isinstance(tag, dict) else tag).strip()
            ],
            "source_kind": "mealie",
            "read_only": True,
        }


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

"""Поддельные Kaiten и Telegram Bot API для тестов (настоящие HTTP-серверы на localhost)."""
from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import Config

MY_ID = 777
IN_WORK = 55
NEW_COL = 11


def run(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


def base_env(**overrides) -> dict:
    env = {
        "KAITEN_BASE_URL": "http://kaiten.invalid/api/latest",
        "KAITEN_TOKEN": "t",
        "KAITEN_CARD_URL_PREFIX": "https://x.kaiten.ru/card/",
        "MY_KAITEN_USER_ID": str(MY_ID),
        "IN_WORK_COLUMN_ID": str(IN_WORK),
        "QUEUE_COLUMN_IDS": str(NEW_COL),
        "CLIENT_TG_FIELD": "id_42",
        "CLIENT_LANES": "",
        "TRANSPORT": "business",
        "BUSINESS_BOT_TOKEN": "BIZ",
        "ALERT_CHAT_ID": "1000",
        "WEBHOOK_SECRET": "s3cret-webhook-1",
        "WORK_HOURS": "00:00-23:59",
        "WORK_DAYS": "1-7",
        "HUMAN_DELAY_MIN_S": "40",
        "HUMAN_DELAY_MAX_S": "150",
        "SPEED": "custom",
        "DB_PATH": ":memory:",
        "SLA_MINUTES": "240",
        "SPECIALIST_NAME": "Улугбек",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def make_cfg(**overrides) -> Config:
    return Config.from_env(base_env(**overrides))


def card(card_id=1, responsible=MY_ID, column=NEW_COL, owner=500, client="@Client_One", **extra):
    data = {
        "id": card_id,
        "title": f"Не работает оплата {card_id}",
        "column_id": column,
        "owner_id": owner,
        "members": [{"id": responsible, "type": 2}, {"id": 999, "type": 1}],
        "properties": {"id_42": client} if client is not None else {},
        "board_id": 900,
    }
    data.update(extra)
    return data


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)  # 12:00 в Бишкеке

    def __call__(self) -> datetime:
        return self.now


class FakeKaiten:
    def __init__(self):
        self.cards: dict[int, dict] = {}
        self.patches: list[tuple[int, dict]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_patch = False
        self.fail_comment = False
        self.list_without_members = False
        self.props = [{"id": 42, "name": "Telegram ученика/учителя", "type": "string"},
                      {"id": 43, "name": "Ваш телеграм", "type": "string"},
                      {"id": 44, "name": "Выберите сервис", "type": "select"},
                      {"id": 45, "name": "Фактическая категория проблемы", "type": "select"},
                      {"id": 46, "name": "Фактическая категория проблемы", "type": "string"}]
        self.select_values = {45: [{"id": 501, "value": "Bitrix24"}, {"id": 502, "value": "LMS"},
                                   {"id": 503, "value": "Смена способа оплаты"},
                                   {"id": 504, "value": "Камера/Микрофон"}, {"id": 505, "value": "Старое", "deleted": True},
                                   {"id": 506, "value": "Решилось само/неактуально"}]}
        self.props_calls = 0
        self.valid_token = "kaiten-token-123"
        self.board = {"id": 900, "columns": [
            {"id": NEW_COL, "title": "Очередь"}, {"id": IN_WORK, "title": "В работе"},
            {"id": 70, "title": "В ожидании", "subcolumns": [{"id": 77, "title": "Ждем ответа"},
                                                             {"id": 78, "title": "Проблемные"},
                                                             {"id": 79, "title": "Повторно связаться"}]},
            {"id": 90, "title": "Готово"}]}
        self.list_calls: list[dict] = []
        self.card_comments: dict[int, list[dict]] = {}  # комментарии, которые уже были в карточке
        self.comment_list_calls: list[int] = []
        self.fail_comment_list: set[int] = set()
        self.get_calls: list[int] = []
        app = web.Application()
        app.router.add_get("/api/latest/cards", self._list)
        app.router.add_get("/api/latest/users/current", self._me)
        app.router.add_get("/api/latest/boards/{id}", self._board)
        app.router.add_get("/api/latest/company/custom-properties", self._props)
        app.router.add_get("/api/latest/company/custom-properties/{id}/select-values", self._select_values)
        app.router.add_get("/api/latest/cards/{id}", self._get)
        app.router.add_patch("/api/latest/cards/{id}", self._patch)
        app.router.add_post("/api/latest/cards/{id}/comments", self._comment)
        app.router.add_get("/api/latest/cards/{id}/comments", self._list_comments)
        self.server = TestServer(app)

    async def start(self) -> str:
        await self.server.start_server()
        return str(self.server.make_url("/api/latest"))

    async def close(self):
        await self.server.close()

    async def _me(self, request):
        other = getattr(self, "users_by_token", {}).get(request.headers.get("Authorization", "")[7:])
        if other is not None:
            return web.json_response({"id": other, "full_name": "Коллега"})
        if request.headers.get("Authorization") != f"Bearer {self.valid_token}":
            return web.json_response({"message": "Unauthorized"}, status=401)
        return web.json_response({"id": MY_ID, "full_name": "Улугбек Маткаримов"})

    async def _board(self, request):
        return web.json_response(self.board)

    async def _props(self, request):
        self.props_calls += 1
        if getattr(self, "fail_props", False):
            return web.json_response({"message": "boom"}, status=500)
        limit, offset = int(request.query.get("limit", 100)), int(request.query.get("offset", 0))
        return web.json_response(self.props[offset:offset + limit])

    async def _select_values(self, request):
        return web.json_response(self.select_values.get(int(request.match_info["id"]), []))

    async def _list(self, request):
        q = request.query
        self.list_calls.append(dict(q))
        column = int(q["column_id"])
        limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
        rows = [dict(c) for c in sorted(self.cards.values(), key=lambda c: c["id"]) if c["column_id"] == column]
        if self.list_without_members:
            for row in rows:
                row.pop("members", None)
        return web.json_response(rows[offset:offset + limit])

    async def _get(self, request):
        cid = int(request.match_info["id"])
        self.get_calls.append(cid)
        if cid not in self.cards:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response(self.cards[cid])

    async def _patch(self, request):
        cid = int(request.match_info["id"])
        if self.fail_patch:
            return web.json_response({"message": "forbidden"}, status=403)
        body = await request.json()
        self.patches.append((cid, body))
        body = dict(body)
        if "properties" in body:  # как в Kaiten: меняются только переданные поля
            merged = dict(self.cards[cid].get("properties") or {})
            merged.update(body.pop("properties"))
            self.cards[cid]["properties"] = merged
        self.cards[cid].update(body)
        return web.json_response(self.cards[cid])

    async def _list_comments(self, request):
        cid = int(request.match_info["id"])
        self.comment_list_calls.append(cid)
        if cid in self.fail_comment_list:
            return web.json_response({"message": "boom"}, status=400)
        if cid not in self.cards and cid not in self.card_comments:
            return web.json_response({"message": "not found"}, status=404)
        posted = [{"id": 10_000 + i, "text": t, "created": "2026-09-23T06:00:00.000Z"}
                  for i, (c, t) in enumerate(self.comments) if c == cid]
        return web.json_response([*self.card_comments.get(cid, []), *posted])

    async def _comment(self, request):
        cid = int(request.match_info["id"])
        if self.fail_comment:
            return web.json_response({"message": "comments are temporarily unavailable"}, status=403)
        body = await request.json()
        self.comments.append((cid, body["text"]))
        return web.json_response({"id": 1, "text": body["text"]})


class FakeBotApi:
    """Поддельный api.telegram.org: пишет все вызовы, отдаёт заранее заданные обновления."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []  # (token, method, params)
        self.updates: list[dict] = []
        self.update_requests: list[dict] = []
        self.updates_token: str | None = None  # у каждого бота свои обновления
        self.errors: dict[str, dict] = {}
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        self.server = TestServer(app)

    async def start(self) -> str:
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def close(self):
        await self.server.close()

    def methods(self, token: str | None = None) -> list[str]:
        return [m for t, m, _ in self.calls if token is None or t == token]

    def params(self, method: str) -> list[dict]:
        return [p for _, m, p in self.calls if m == method]

    async def _handle(self, request):
        token, method = request.match_info["token"], request.match_info["method"]
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            params = {k: (v.file.read() if hasattr(v, "file") else v) for k, v in form.items()}
            params.update({f"{k}_name": v.filename for k, v in form.items() if hasattr(v, "filename")})
        else:
            params = await request.json() if request.can_read_body else {}
        if method == "getUpdates":
            self.update_requests.append(params)
            if not self.updates or (self.updates_token and token != self.updates_token):
                await asyncio.sleep(0.05)
                return web.json_response({"ok": True, "result": []})
            offset = params.get("offset") or 0
            result = [u for u in self.updates if u["update_id"] >= offset]
            self.updates = []
            return web.json_response({"ok": True, "result": result})
        self.calls.append((token, method, params))
        if method in self.errors:
            return web.json_response(self.errors[method])
        if method == "sendMessage":
            return web.json_response({"ok": True, "result": {"message_id": len(self.calls)}})
        return web.json_response({"ok": True, "result": True})


def business_connection_update(update_id=1, enabled=True, can_reply=True):
    return {"update_id": update_id, "business_connection": {
        "id": "BC1", "user": {"id": 42}, "user_chat_id": 42, "date": 0,
        "rights": {"can_reply": can_reply}, "is_enabled": enabled}}


def incoming_message_update(update_id=2, chat_id=500112345, username="Client_One", when: datetime | None = None,
                            message_id=10):
    ts = int((when or datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)).timestamp())
    return {"update_id": update_id, "business_message": {
        "business_connection_id": "BC1", "message_id": message_id, "date": ts,
        "chat": {"id": chat_id, "type": "private", "username": username},
        "from": {"id": chat_id, "username": username}, "text": "здравствуйте, не проходит оплата"}}

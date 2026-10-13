"""Минимальный клиент Kaiten API (карточка, перенос в колонку, комментарий)."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)


class KaitenError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class KaitenClient:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession,
                 retries: int = 3, backoff_s: float = 1.5):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.retries = retries
        self.backoff_s = backoff_s
        self.list_ttl_s = 0.0   # >0: короткий кеш списков колонок (несколько специалистов опрашивают одно)
        self._lists: dict[tuple, tuple[float, list[dict]]] = {}

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict | list | None:
        url = f"{self.base_url}{path}"
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                async with self.session.request(method, url, json=json, headers=self.headers,
                                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status == 429 or resp.status >= 500:
                        last_error = f"{method} {path}: HTTP {resp.status}"
                        await asyncio.sleep(self.backoff_s * attempt)
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        raise KaitenError(f"{method} {path}: HTTP {resp.status}: {body}", resp.status)
                    if resp.content_length == 0:
                        return {}
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{method} {path}: {exc!r}"
                await asyncio.sleep(self.backoff_s * attempt)
        raise KaitenError(last_error or f"{method} {path}: не удалось")

    async def get_card(self, card_id: int) -> dict | None:
        """Карточка или None, если её нет или к ней больше нет доступа (удалили, архивировали, перенесли)."""
        try:
            data = await self._request("GET", f"/cards/{card_id}")
        except KaitenError as exc:
            if exc.status == 403:
                return None
            raise
        return data if isinstance(data, dict) and data.get("id") else None

    async def list_cards(self, column_id: int, page_size: int = 100, max_pages: int = 10) -> list[dict]:
        """Все карточки колонки (с постраничной загрузкой)."""
        key = (column_id, page_size, max_pages)
        if self.list_ttl_s > 0:
            import time
            hit = self._lists.get(key)
            if hit and time.monotonic() - hit[0] < self.list_ttl_s:
                return [dict(c) for c in hit[1]]
        cards = await self._list_cards(column_id, page_size, max_pages)
        if self.list_ttl_s > 0:
            import time
            self._lists[key] = (time.monotonic(), cards)
            return [dict(c) for c in cards]
        return cards

    async def _list_cards(self, column_id: int, page_size: int, max_pages: int) -> list[dict]:
        cards: list[dict] = []
        for page in range(max_pages):
            query = urlencode({"column_id": column_id, "limit": page_size, "offset": page * page_size})
            data = await self._request("GET", f"/cards?{query}")
            batch = [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []
            cards.extend(batch)
            if len(batch) < page_size:
                break
        return cards

    async def list_custom_properties(self, page_size: int = 100, max_pages: int = 20) -> list[dict]:
        """Все поля карточек компании: [{id, name, type, ...}]."""
        props: list[dict] = []
        for page in range(max_pages):
            query = urlencode({"limit": page_size, "offset": page * page_size})
            data = await self._request("GET", f"/company/custom-properties?{query}")
            batch = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
            props.extend(batch)
            if len(batch) < page_size:
                break
        return props

    async def list_select_values(self, property_id: int) -> list[dict]:
        """Варианты поля-списка: [{id, value}]."""
        data = await self._request("GET", f"/company/custom-properties/{property_id}/select-values")
        return [v for v in data if isinstance(v, dict)] if isinstance(data, list) else []

    async def set_properties(self, card_id: int, properties: dict) -> None:
        """Заполнить поля карточки: {"id_123": значение}."""
        self._lists.clear()
        await self._request("PATCH", f"/cards/{card_id}", json={"properties": properties})

    async def get_board(self, board_id: int) -> dict | None:
        data = await self._request("GET", f"/boards/{board_id}")
        return data if isinstance(data, dict) else None

    async def current_user(self) -> dict | None:
        data = await self._request("GET", "/users/current")
        return data if isinstance(data, dict) else None

    async def move_card(self, card_id: int, column_id: int, board_id: int | None = None) -> None:
        self._lists.clear()  # карточка переехала — списки колонок устарели
        body: dict = {"column_id": column_id}
        if board_id:
            body["board_id"] = board_id
        await self._request("PATCH", f"/cards/{card_id}", json=body)

    async def add_comment(self, card_id: int, text: str) -> None:
        await self._request("POST", f"/cards/{card_id}/comments", json={"text": text})

    async def list_comments(self, card_id: int) -> list[dict]:
        """Комментарии карточки: [{id, text, created, ...}]; [] если карточки нет."""
        try:
            data = await self._request("GET", f"/cards/{card_id}/comments")
        except KaitenError as exc:
            if exc.status == 403:
                return []
            raise
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

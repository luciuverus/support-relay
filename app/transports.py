"""Отправка сообщений от корпоративного аккаунта и уведомлений лично тебе.

business — официальный Telegram Business: бот подключён к корпоративному аккаунту
           и пишет от его имени. Нужен Telegram Premium на аккаунте. Писать можно
           только в чаты, где собеседник сам писал за последние 24 часа.
userbot  — сессия самого аккаунта через MTProto (Telethon). Без ограничения 24 ч,
           но это неофициальная автоматизация: есть риск ограничений аккаунта.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import aiohttp

from .humanize import typing_seconds
from .logic import Contact
from .storage import Storage

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]
WINDOW = timedelta(hours=24) - timedelta(minutes=5)  # запас, чтобы не упереться в границу


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SendError(RuntimeError):
    """reason: "unknown_chat" — бот ещё не видел этот чат; "window" — прошло больше 24 ч; иначе ""."""

    def __init__(self, message: str, retry_after_s: float | None = None, reason: str = ""):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.reason = reason


class SendCancelled(RuntimeError):
    """Сообщение отменили, пока бот «печатал» (ты нажал «Отправил вручную» или написал сам)."""


StillWanted = Callable[[], bool]


def _stop_if_cancelled(still_wanted: "StillWanted | None") -> None:
    if still_wanted and not still_wanted():
        raise SendCancelled("отменено во время «печатает…»")


# (ключи чата, входящее?, написал ли ты сам вручную?, когда)
ActivityHandler = Callable[[list, bool, bool, datetime], Awaitable[None]]
UpdateHandler = Callable[[dict], Awaitable[None]]


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, description: str, code: int | None, retry_after: float | None):
        super().__init__(f"{method}: {description}")
        self.code = code
        self.retry_after = retry_after


class BotApi:
    def __init__(self, token: str, session: aiohttp.ClientSession, base: str = "https://api.telegram.org"):
        self.url = f"{base.rstrip('/')}/bot{token}"
        self.session = session

    async def call(self, method: str, timeout_s: float = 30, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            async with self.session.post(f"{self.url}/{method}", json=payload,
                                         timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise TelegramApiError(method, repr(exc), None, None) from exc
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TelegramApiError(method, str(data.get("description", "unknown error")),
                                   data.get("error_code"), retry)
        return data.get("result")


    async def send_document(self, chat_id: str, filename: str, data: bytes, caption: str = "",
                            timeout_s: float = 60):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)
        form.add_field("document", data, filename=filename, content_type="text/csv")
        try:
            async with self.session.post(f"{self.url}/sendDocument", data=form,
                                         timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                result = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise TelegramApiError("sendDocument", repr(exc), None, None) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            result = result if isinstance(result, dict) else {}
            raise TelegramApiError("sendDocument", str(result.get("description", "unknown error")),
                                   result.get("error_code"), None)
        return result.get("result")


class AlertBot:
    """Служебные уведомления лично тебе (новая заявка, SLA, ошибки отправки)."""

    def __init__(self, api: BotApi | None, chat_id: str, dry_run: bool = False):
        self.api = api
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.sent: list[str] = []
        self.sent_buttons: list[list | None] = []
        self.files: list[tuple[str, bytes]] = []

    async def send(self, text: str, buttons: list[list[dict]] | None = None) -> None:
        """buttons: строки кнопок, кнопка = {"text", "url"} или {"text", "callback_data"}."""
        self.sent.append(text)
        self.sent_buttons.append(buttons)
        if self.dry_run or self.api is None:
            log.info("[alert] %s", text)
            return
        markup = {"inline_keyboard": buttons} if buttons else None
        try:
            await self.api.call("sendMessage", chat_id=self.chat_id, text=text,
                                disable_web_page_preview=True, reply_markup=markup)
        except TelegramApiError as exc:
            log.error("Не удалось отправить уведомление себе: %s", exc)
            if markup:  # вдруг кнопка не понравилась Telegram — шлём хотя бы текст
                try:
                    await self.api.call("sendMessage", chat_id=self.chat_id, text=text,
                                        disable_web_page_preview=True)
                except TelegramApiError:
                    pass


    async def send_file(self, filename: str, data: bytes, caption: str = "") -> bool:
        """Файл лично тебе (выгрузка CSV). False — не получилось, скажи путь к файлу текстом."""
        self.files.append((filename, data))
        if self.dry_run or self.api is None:
            log.info("[alert] файл %s (%d байт)", filename, len(data))
            return True
        try:
            await self.api.send_document(self.chat_id, filename, data, caption)
            return True
        except TelegramApiError as exc:
            log.error("Не удалось отправить файл себе: %s", exc)
            return False


class UpdatePoller:
    """Читает обновления бота (getUpdates) и отдаёт их обработчику; позиция хранится в базе."""

    def __init__(self, api: BotApi, storage: Storage, offset_key: str, allowed: list[str],
                 handler: UpdateHandler, sleep: Sleep = asyncio.sleep):
        self.api, self.storage, self.offset_key = api, storage, offset_key
        self.allowed, self.handler, self.sleep = allowed, handler, sleep
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name=f"updates-{self.offset_key}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        offset = int(self.storage.get(self.offset_key, "0") or 0)
        while True:
            try:
                updates = await self.api.call("getUpdates", timeout_s=40, offset=offset or None,
                                              timeout=25, allowed_updates=self.allowed)
                for update in updates or []:
                    offset = int(update["update_id"]) + 1
                    try:
                        await self.handler(update)
                    except Exception:  # noqa: BLE001 - одно плохое обновление не останавливает приём
                        log.exception("Ошибка обработки обновления Telegram")
                    self.storage.set(self.offset_key, str(offset))
            except asyncio.CancelledError:
                raise
            except TelegramApiError as exc:
                if exc.code == 409:
                    log.error("Бот уже используется в другом месте (webhook/другой процесс). "
                              "Для этого сервиса нужен отдельный бот.")
                    await self.sleep(30)
                else:
                    log.warning("getUpdates: %s", exc)
                    await self.sleep(5)
            except Exception:  # noqa: BLE001 - приём не должен умирать
                log.exception("Ошибка приёма обновлений Telegram")
                await self.sleep(5)


class Transport:
    name = "base"
    cps: tuple[float, float] = (4.0, 7.0)
    rng: random.Random
    # () -> (cps_min, cps_max, cap): текущий темп из /speed; None — self.cps и потолок 45 с
    pace: Callable[[], tuple[float, float, float]] | None = None

    def typing_time(self, text: str) -> float:
        cps_min, cps_max, cap = self.pace() if self.pace else (*self.cps, 45.0)
        return typing_seconds(text, self.rng, cps_min, cps_max, cap)

    async def start(self) -> None:  # pragma: no cover - интерфейс
        pass

    async def stop(self) -> None:  # pragma: no cover - интерфейс
        pass

    async def send(self, contact: Contact, text: str,
                   still_wanted: StillWanted | None = None) -> None:  # pragma: no cover - интерфейс
        """still_wanted() проверяется прямо перед отправкой: False — не отправлять (SendCancelled)."""
        raise NotImplementedError


class BusinessTransport(Transport):
    name = "business"

    def __init__(self, api: BotApi, storage: Storage, rng: random.Random,
                 cps_min: float, cps_max: float, sleep: Sleep = asyncio.sleep,
                 clock: Clock = utcnow, alert: AlertBot | None = None):
        self.api = api
        self.storage = storage
        self.rng = rng
        self.cps = (cps_min, cps_max)
        self.sleep = sleep
        self.clock = clock
        self.alert = alert
        self.extra_handler: UpdateHandler | None = None   # команды и кнопки для тебя (тот же бот)
        self.on_activity: ActivityHandler | None = None    # кто-то написал / ты ответил вручную
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._recent: dict[int, list[tuple[datetime, str]]] = {}  # что мы сами только что отправили
        self._poller = UpdatePoller(api, storage, "tg_offset",
                                    ["business_connection", "business_message", "edited_business_message",
                                     "message", "callback_query"], self.handle_update, sleep)

    # --- приём обновлений: подключение к аккаунту и входящие сообщения ---
    async def start(self) -> None:
        self._poller.start()
        if not self.storage.get("business_connection_id") and self.alert:
            await self.alert.send(
                "ℹ️ Сервис запущен, но бот пока не подключён к корпоративному аккаунту, "
                "поэтому писать людям он не сможет. В корпоративном аккаунте: Настройки → "
                "Telegram для бизнеса → Чат-боты → добавь бота (если уже добавлен — удали и добавь "
                "заново) и разреши отвечать. Когда получится, я напишу «Бот подключён».")

    async def stop(self) -> None:
        await self._poller.stop()

    def _is_our_echo(self, chat_id: int, text: str) -> bool:
        now = self.clock()
        recent = [(t, x) for t, x in self._recent.get(chat_id, []) if now - t < timedelta(minutes=5)]
        self._recent[chat_id] = recent
        return any(x == text for _, x in recent)

    async def handle_update(self, update: dict) -> None:
        if "message" in update and await self._from_account_itself(update["message"] or {}):
            return
        if "message" in update or "callback_query" in update:
            if self.extra_handler:
                await self.extra_handler(update)
            return
        conn = update.get("business_connection")
        if conn:
            rights = conn.get("rights") or {}
            can_reply = rights.get("can_reply", conn.get("can_reply", False))
            enabled = bool(conn.get("is_enabled")) and bool(can_reply)
            self.storage.set("business_connection_id", conn["id"] if enabled else "")
            self.storage.set("business_owner_id", str((conn.get("user") or {}).get("id", "")))
            if conn.get("user_chat_id"):
                self.storage.set("business_owner_chat", str(conn["user_chat_id"]))
            if self.alert:
                await self.alert.send(
                    "✅ Бот подключён к корпоративному аккаунту и может отвечать."
                    if enabled else
                    "⚠️ Бот отключён от корпоративного аккаунта или у него нет права отвечать. "
                    "Отправка сообщений остановлена."
                )
            return
        msg = update.get("business_message") or update.get("edited_business_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        incoming = sender.get("id") == chat_id  # в личке входящее = от собеседника
        keys = [f"id:{chat_id}"]
        if chat.get("username"):
            keys.append(f"username:{chat['username'].lower()}")
        at = datetime.fromtimestamp(msg.get("date", 0), tz=timezone.utc) if msg.get("date") else self.clock()
        # Исходящее без sender_business_bot и не совпадающее с тем, что мы только что отправили, —
        # это ты ответил сам (с телефона или компьютера).
        manual = (not incoming and "edited_business_message" not in update
                  and not msg.get("sender_business_bot")
                  and not self._is_our_echo(int(chat_id), str(msg.get("text") or "")))
        name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) or None
        self.storage.remember_chat(keys, int(chat_id), at if incoming else None,
                                   msg.get("message_id") if incoming else None, name,
                                   last_out_at=at if manual else None)
        if self.on_activity and (incoming or manual) and "edited_business_message" not in update:
            await self.on_activity(keys, incoming, manual, at, text=str(msg.get("text") or msg.get("caption") or ""))

    # --- чат самого корпоративного аккаунта с ботом ---
    ACCOUNT_READY = ("✅ Готово: этот чат подключён. Когда бот не сможет написать человеку первым, сюда придёт "
                     "готовый текст с кнопкой «✍️ Открыть чат с готовым текстом» — нажми её отсюда, и чат откроется "
                     "сразу от корпоративного аккаунта. Когда сообщение уйдёт, бот уберёт его отсюда сам.")

    async def _from_account_itself(self, msg: dict) -> bool:
        """Сам корпоративный аккаунт пишет боту (/start) — ответить, что чат подключён. True — обработали."""
        chat = msg.get("chat") or {}
        owner = self.owner_chat()
        if owner is None or chat.get("id") != owner or chat.get("type") != "private":
            return False
        self.storage.set("corp_chat_warned", "")
        try:
            await self.api.call("sendMessage", chat_id=owner, text=self.ACCOUNT_READY)
        except TelegramApiError as exc:
            log.warning("Не смог ответить корпоративному аккаунту: %s", exc)
        return True

    def owner_chat(self) -> int | None:
        """Личный чат корпоративного аккаунта с ботом: туда — готовые тексты, чтобы кнопка открывала чат
        с клиентом уже в корпоративном аккаунте, а не в личном аккаунте специалиста."""
        value = self.storage.get("business_owner_chat", "") or self.storage.get("business_owner_id", "") or ""
        return int(value) if value.lstrip("-").isdigit() else None

    async def notify_owner(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        """Сообщение в чат корпоративного аккаунта с ботом. Номер сообщения или None, если не вышло."""
        chat = self.owner_chat()
        if chat is None:
            return None
        markup = {"inline_keyboard": buttons} if buttons else None
        try:
            result = await self.api.call("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True,
                                         reply_markup=markup)
        except TelegramApiError as exc:
            log.warning("Не смог написать в чат корпоративного аккаунта с ботом: %s", exc)
            return None
        return result.get("message_id") if isinstance(result, dict) else None

    async def delete_owner_message(self, message_id: int) -> None:
        chat = self.owner_chat()
        if chat is None or not message_id:
            return
        try:
            await self.api.call("deleteMessage", chat_id=chat, message_id=message_id)
        except TelegramApiError as exc:
            log.debug("deleteMessage в чате аккаунта: %s", exc)

    # --- отправка ---
    async def send(self, contact: Contact, text: str, still_wanted: StillWanted | None = None) -> None:
        connection_id = self.storage.get("business_connection_id", "")
        if not connection_id:
            raise SendError("Бот не подключён к корпоративному аккаунту "
                            "(Настройки → Telegram для бизнеса → Чат-боты).")
        if contact.kind == "phone":
            raise SendError("Business-бот не умеет писать по номеру телефона — "
                            "укажи в карточке @username или chat id.")
        chat = self.storage.find_chat(contact.key)
        if not chat:
            raise SendError(f"{contact.value}: этот человек ещё не писал на корпоративный аккаунт "
                            "с момента подключения бота — бот не знает его чат.", reason="unknown_chat")
        last_in = chat["last_in_at"]
        if not last_in or self.clock() - last_in > WINDOW:
            raise SendError(f"{contact.value}: собеседник не писал больше 24 часов — Telegram не даёт "
                            "боту писать первым.", reason="window")
        chat_id = chat["chat_id"]
        # несколько специалистов на одном аккаунте: в один чат — по одному сообщению за раз
        lock = self._chat_locks.setdefault(int(chat_id), asyncio.Lock())
        async with lock:
            await self._send_to(connection_id, chat, chat_id, text, still_wanted)

    async def _send_to(self, connection_id: str, chat: dict, chat_id, text: str,
                       still_wanted: StillWanted | None) -> None:
        try:
            if chat.get("last_msg_id"):
                try:
                    await self.api.call("readBusinessMessage", business_connection_id=connection_id,
                                        chat_id=chat_id, message_id=chat["last_msg_id"])
                except TelegramApiError as exc:  # нет права can_read_messages — не критично
                    log.debug("readBusinessMessage: %s", exc)
            remaining = self.typing_time(text)
            while remaining > 0:
                await self.api.call("sendChatAction", business_connection_id=connection_id,
                                    chat_id=chat_id, action="typing")
                step = min(4.5, remaining)  # статус «печатает» держится ~5 секунд
                await self.sleep(step)
                remaining -= step
                _stop_if_cancelled(still_wanted)  # отменили — перестаём «печатать» сразу
            _stop_if_cancelled(still_wanted)
            self._recent.setdefault(int(chat_id), []).append((self.clock(), text))
            await self.api.call("sendMessage", business_connection_id=connection_id,
                                chat_id=chat_id, text=text)
        except TelegramApiError as exc:
            raise SendError(f"Telegram: {exc}", exc.retry_after) from exc


class UserbotTransport(Transport):
    """Отправка через сессию самого аккаунта (Telethon)."""

    name = "userbot"

    def __init__(self, client, rng: random.Random, cps_min: float, cps_max: float,
                 sleep: Sleep = asyncio.sleep):
        self.client = client
        self.rng = rng
        self.cps = (cps_min, cps_max)
        self.sleep = sleep

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Сессия аккаунта не авторизована: сначала запусти login_userbot.py")

    async def stop(self) -> None:
        await self.client.disconnect()

    async def send(self, contact: Contact, text: str, still_wanted: StillWanted | None = None) -> None:
        from telethon import errors  # импорт здесь, чтобы business-режим не требовал telethon

        target: int | str
        if contact.kind == "id":
            target = int(contact.value)
        elif contact.kind == "username":
            target = "@" + contact.value
        else:
            target = contact.value
        try:
            entity = await self.client.get_entity(target)
        except (ValueError, TypeError, errors.RPCError) as exc:
            raise SendError(f"{contact.value}: не нашёл такой чат ({exc}). "
                            "По номеру телефона ищется только среди контактов аккаунта.") from exc
        try:
            try:
                await self.client.send_read_acknowledge(entity)
            except errors.RPCError as exc:
                log.debug("send_read_acknowledge: %s", exc)
            async with self.client.action(entity, "typing"):
                await self.sleep(self.typing_time(text))
            _stop_if_cancelled(still_wanted)
            await self.client.send_message(entity, text)
        except errors.FloodWaitError as exc:
            raise SendError(f"Telegram просит подождать {exc.seconds} с", float(exc.seconds)) from exc
        except errors.RPCError as exc:
            raise SendError(f"Telegram: {exc}") from exc

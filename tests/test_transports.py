import asyncio
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from telethon import errors

from app.logic import normalize_contact
from app.storage import Storage
from app.transports import AlertBot, BotApi, BusinessTransport, SendError, UserbotTransport
from tests.fakes import (Clock, FakeBotApi, business_connection_update, incoming_message_update, run)

T0 = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)


async def no_sleep(_):
    return None


class BizEnv:
    async def __aenter__(self):
        self.bot = FakeBotApi()
        base = await self.bot.start()
        self.session = aiohttp.ClientSession()
        self.storage = Storage(":memory:")
        self.clock = Clock(T0)
        self.alert = AlertBot(None, "1")
        self.t = BusinessTransport(BotApi("BIZ", self.session, base), self.storage, random.Random(0),
                                   4, 7, sleep=no_sleep, clock=self.clock, alert=self.alert)
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.bot.close()


@run
async def test_business_sends_as_account_with_typing_and_read():
    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update(when=T0 - timedelta(hours=1)))
        await e.t.send(normalize_contact("@client_one"), "Добрый день! Уже разбираемся")
        methods = e.bot.methods()
        assert methods[0] == "readBusinessMessage"
        assert methods[-1] == "sendMessage" and "sendChatAction" in methods
        for _, _, params in e.bot.calls:
            assert params["business_connection_id"] == "BC1" and params["chat_id"] == 500112345
        assert e.bot.params("sendMessage")[0]["text"] == "Добрый день! Уже разбираемся"
        assert "подключён" in e.alert.sent[0]


@run
async def test_business_refuses_outside_24h_window():
    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update(when=T0 - timedelta(hours=25)))
        with pytest.raises(SendError, match="24"):
            await e.t.send(normalize_contact("500112345"), "текст")
        assert "sendMessage" not in e.bot.methods()


@run
async def test_business_only_counts_incoming_messages_for_window():
    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update(when=T0 - timedelta(hours=30)))
        outgoing = incoming_message_update(update_id=3, when=T0 - timedelta(minutes=5))
        outgoing["business_message"]["from"] = {"id": 42}  # это написали мы сами
        await e.t.handle_update(outgoing)
        with pytest.raises(SendError, match="24"):
            await e.t.send(normalize_contact("@client_one"), "текст")


@run
async def test_business_errors_are_explicit():
    async with BizEnv() as e:
        with pytest.raises(SendError, match="не подключён"):
            await e.t.send(normalize_contact("@client_one"), "x")
        await e.t.handle_update(business_connection_update())
        with pytest.raises(SendError, match="не писал"):
            await e.t.send(normalize_contact("@stranger"), "x")
        with pytest.raises(SendError, match="номеру"):
            await e.t.send(normalize_contact("+996555123456"), "x")
        await e.t.handle_update(business_connection_update(update_id=5, can_reply=False))
        with pytest.raises(SendError, match="не подключён"):
            await e.t.send(normalize_contact("@client_one"), "x")


@run
async def test_business_telegram_error_becomes_send_error_with_retry():
    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update(when=T0 - timedelta(minutes=10)))
        e.bot.errors["sendMessage"] = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                       "parameters": {"retry_after": 12}}
        with pytest.raises(SendError) as info:
            await e.t.send(normalize_contact("@client_one"), "x")
        assert info.value.retry_after_s == 12


@run
async def test_business_poller_consumes_updates_and_persists_offset():
    async with BizEnv() as e:
        e.bot.updates = [business_connection_update(update_id=7),
                         incoming_message_update(update_id=8, when=T0)]
        await e.t.start()
        for _ in range(100):
            if e.storage.get("tg_offset") == "9":
                break
            await asyncio.sleep(0.02)
        await e.t.stop()
        assert e.storage.get("tg_offset") == "9"
        assert e.storage.find_chat("username:client_one")["chat_id"] == 500112345


class FakeTelethon:
    def __init__(self, authorized=True, flood=False, unknown=False):
        self.authorized, self.flood, self.unknown = authorized, flood, unknown
        self.log: list[tuple] = []

    async def connect(self):
        self.log.append(("connect",))

    async def disconnect(self):
        self.log.append(("disconnect",))

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, target):
        if self.unknown:
            raise ValueError("No user has that username")
        self.log.append(("get_entity", target))
        return {"entity": target}

    async def send_read_acknowledge(self, entity):
        self.log.append(("read", entity["entity"]))

    @asynccontextmanager
    async def action(self, entity, kind):
        self.log.append(("action_start", kind))
        yield
        self.log.append(("action_end", kind))

    async def send_message(self, entity, text):
        if self.flood:
            raise errors.FloodWaitError(request=None, capture=30)
        self.log.append(("send", entity["entity"], text))


@run
async def test_userbot_reads_types_then_sends():
    client = FakeTelethon()
    t = UserbotTransport(client, random.Random(0), 4, 7, sleep=no_sleep)
    await t.start()
    await t.send(normalize_contact("t.me/Client_One"), "привет")
    await t.send(normalize_contact("500112345"), "ещё")
    names = [entry[0] for entry in client.log]
    assert names[:6] == ["connect", "get_entity", "read", "action_start", "action_end", "send"]
    assert ("get_entity", "@client_one") in client.log and ("get_entity", 500112345) in client.log
    assert ("send", "@client_one", "привет") in client.log


@run
async def test_userbot_errors():
    with pytest.raises(RuntimeError, match="login_userbot"):
        await UserbotTransport(FakeTelethon(authorized=False), random.Random(0), 4, 7).start()
    t = UserbotTransport(FakeTelethon(unknown=True), random.Random(0), 4, 7, sleep=no_sleep)
    with pytest.raises(SendError, match="не нашёл"):
        await t.send(normalize_contact("@nobody_here"), "x")
    t = UserbotTransport(FakeTelethon(flood=True), random.Random(0), 4, 7, sleep=no_sleep)
    with pytest.raises(SendError) as info:
        await t.send(normalize_contact("@client_one"), "x")
    assert info.value.retry_after_s == 30

"""Сквозной тест: настоящий HTTP-сервер приложения + поддельные Kaiten и Telegram."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from app import main as app_main
from app import service, transports
from app.config import Config
from tests.fakes import (IN_WORK, FakeBotApi, FakeKaiten, base_env, business_connection_update, card,
                         incoming_message_update, run)


async def wait_for(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@run
async def test_webhook_to_business_message_end_to_end():
    import tempfile
    tmp = tempfile.mkdtemp()
    employees = f"{tmp}/employees.json"
    with open(employees, "w", encoding="utf-8") as fh:
        json.dump({"500": "@author_person"}, fh)

    app_main.SEND_INTERVAL_S = 0.05
    app_main.SLA_INTERVAL_S = 0.05
    original_stagger = service.STAGGER_S
    service.STAGGER_S = (0.0, 0.0)
    original_typing = transports.typing_seconds
    transports.typing_seconds = lambda *a, **k: 0.01
    kaiten, bot = FakeKaiten(), FakeBotApi()
    try:
        kaiten_base = await kaiten.start()
        bot_base = await bot.start()
        now_update = incoming_message_update(update_id=2)
        import time
        now_update["business_message"]["date"] = int(time.time()) - 60
        bot.updates = [business_connection_update(update_id=1), now_update]
        bot.updates_token = "BIZ"
        cfg = Config.from_env(base_env(
            KAITEN_BASE_URL=kaiten_base, TELEGRAM_API_BASE=bot_base, DB_PATH=f"{tmp}/state.db",
            EMPLOYEES_FILE=employees, MESSAGES_FILE="config/messages.default.json",
            HUMAN_DELAY_MIN_S="0", HUMAN_DELAY_MAX_S="0", ALERT_BOT_TOKEN="ALERT"))
        kaiten.cards[1] = card(1)
        client = TestClient(TestServer(app_main.create_app(cfg)))
        await client.start_server()
        try:
            assert (await client.get("/health")).status == 200
            assert (await client.post("/kaiten/wrong", json={"data": {"id": 1}})).status == 404
            assert await wait_for(lambda: bot.params("sendMessage"))  # алерт о подключении
            resp = await client.post("/kaiten/s3cret-webhook-1", json={"event": "card:update", "data": {"id": 1}})
            assert resp.status == 200
            assert await wait_for(lambda: kaiten.patches == [(1, {"column_id": IN_WORK})])
            # клиенту — от имени аккаунта (business_connection_id), тебе — обычным ботом
            assert await wait_for(lambda: any(p.get("business_connection_id") == "BC1"
                                              for p in bot.params("sendMessage")))
            biz = [p for t, m, p in bot.calls if m == "sendMessage" and p.get("business_connection_id")]
            assert len(biz) == 1 and biz[0]["chat_id"] == 500112345
            alerts = [p["text"] for t, m, p in bot.calls if t == "ALERT" and m == "sendMessage"]
            assert any("Не работает оплата 1" in a for a in alerts)
            # автор не писал на аккаунт -> честное предупреждение тебе, а не молчание
            assert await wait_for(lambda: any("author_person" in p["text"] for t, m, p in bot.calls
                                              if t == "ALERT" and m == "sendMessage"))
        finally:
            await client.close()
    finally:
        transports.typing_seconds = original_typing
        service.STAGGER_S = original_stagger
        app_main.SEND_INTERVAL_S, app_main.SLA_INTERVAL_S = 5, 30
        await kaiten.close()
        await bot.close()


@run
async def test_polling_mode_without_webhook_end_to_end():
    """Нет прав администратора Kaiten: вебхука нет, сервис сам опрашивает «Очередь»."""
    import tempfile
    tmp = tempfile.mkdtemp()
    kaiten, bot = FakeKaiten(), FakeBotApi()
    try:
        kaiten_base = await kaiten.start()
        bot_base = await bot.start()
        cfg = Config.from_env(base_env(
            KAITEN_BASE_URL=kaiten_base, TELEGRAM_API_BASE=bot_base, DB_PATH=f"{tmp}/state.db",
            EMPLOYEES_FILE=f"{tmp}/none.json", MESSAGES_FILE="config/messages.default.json",
            WEBHOOK_SECRET="", POLL_INTERVAL_S="10", ALERT_BOT_TOKEN="ALERT"))
        kaiten.cards[1] = card(1)
        kaiten.cards[2] = card(2, column=77)  # моя, но в ожидании
        client = TestClient(TestServer(app_main.create_app(cfg)))
        await client.start_server()
        try:
            assert await wait_for(lambda: kaiten.patches == [(1, {"column_id": IN_WORK})])
            assert kaiten.list_calls and kaiten.list_calls[0]["column_id"] == "11"
            assert (await client.post("/kaiten/", json={"data": {"id": 2}})).status == 404
            assert (await client.post("/kaiten/anything", json={"data": {"id": 2}})).status == 404
            assert await wait_for(lambda: any("Не работает оплата 1" in p.get("text", "")
                                              for t, m, p in bot.calls if t == "ALERT"))
            assert all(cid != 2 for cid, _ in kaiten.patches)
        finally:
            await client.close()
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_team_member_from_team_json_works_in_the_real_app():
    """config/team.json: коллега на том же боте берёт свою заявку, уведомление — ему в личку, база — своя."""
    import os
    import tempfile
    tmp = tempfile.mkdtemp()
    team = f"{tmp}/team.json"
    with open(team, "w", encoding="utf-8") as fh:
        json.dump([{"chat_id": "2000", "kaiten_id": 778, "name": "Тимур"}], fh)
    kaiten, bot = FakeKaiten(), FakeBotApi()
    try:
        kaiten_base = await kaiten.start()
        bot_base = await bot.start()
        cfg = Config.from_env(base_env(
            KAITEN_BASE_URL=kaiten_base, TELEGRAM_API_BASE=bot_base, DB_PATH=f"{tmp}/state.db",
            EMPLOYEES_FILE=f"{tmp}/none.json", MESSAGES_FILE="config/messages.default.json",
            WEBHOOK_SECRET="", POLL_INTERVAL_S="10", ALERT_BOT_TOKEN="ALERT", TEAM_FILE=team))
        kaiten.cards[1] = card(1)
        kaiten.cards[2] = card(2, responsible=778, owner=600, client="@Client_Two")
        client = TestClient(TestServer(app_main.create_app(cfg)))
        await client.start_server()
        try:
            assert await wait_for(lambda: {cid for cid, _ in kaiten.patches} == {1, 2})
            to = {(p.get("chat_id"), "оплата 2" in p.get("text", "")) for t, m, p in bot.calls
                  if t == "ALERT" and m == "sendMessage"}
            assert await wait_for(lambda: any(t == "ALERT" and str(p.get("chat_id")) == "2000"
                                              and "оплата 2" in p.get("text", "") for t, m, p in bot.calls))
            assert not any(t == "ALERT" and str(p.get("chat_id")) == "1000" and "оплата 2" in p.get("text", "")
                           for t, m, p in bot.calls)
            assert os.path.exists(f"{tmp}/state-778.db") and to
        finally:
            await client.close()
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_colleague_joins_by_buttons_in_the_real_app_and_starts_working():
    """Коллега пишет боту /start → главный жмёт кнопки → бот сразу работает и на коллегу."""
    import tempfile
    tmp = tempfile.mkdtemp()
    team = f"{tmp}/team.json"
    kaiten, bot = FakeKaiten(), FakeBotApi()
    old_team_interval = app_main.TEAM_INTERVAL_S
    app_main.TEAM_INTERVAL_S = 0.1
    try:
        kaiten_base = await kaiten.start()
        bot_base = await bot.start()
        cfg = Config.from_env(base_env(
            KAITEN_BASE_URL=kaiten_base, TELEGRAM_API_BASE=bot_base, DB_PATH=f"{tmp}/state.db",
            EMPLOYEES_FILE=f"{tmp}/none.json", MESSAGES_FILE="config/messages.default.json",
            WEBHOOK_SECRET="", POLL_INTERVAL_S="10", ALERT_BOT_TOKEN="ALERT", TEAM_FILE=team))
        kaiten.cards[5] = card(5, responsible=778, owner=600, client="@Client_Five",
                               members=[{"id": 778, "type": 2, "full_name": "Тимур Тестов"}])
        bot.updates_token = "ALERT"
        client = TestClient(TestServer(app_main.create_app(cfg)))
        await client.start_server()

        def buttons_to(chat):
            out = []
            for t, m, p in bot.calls:
                if t == "ALERT" and m == "sendMessage" and str(p.get("chat_id")) == chat:
                    for row in (p.get("reply_markup") or {}).get("inline_keyboard", []):
                        out += [b.get("callback_data") for b in row]
            return out
        try:
            await asyncio.sleep(0.3)  # детектор увидел доску: Тимур — ответственный
            bot.updates = [{"update_id": 1, "message": {"message_id": 1, "chat": {"id": 3000, "type": "private"},
                                                        "from": {"id": 3000, "first_name": "Тимур"}, "text": "/start"}}]
            assert await wait_for(lambda: "join:3000" in buttons_to("1000"))
            bot.updates = [{"update_id": 2, "callback_query": {"id": "q1", "from": {"id": 1000}, "data": "join:3000"}}]
            assert await wait_for(lambda: "joinas:3000:778" in buttons_to("1000"))
            bot.updates = [{"update_id": 3, "callback_query": {"id": "q2", "from": {"id": 1000},
                                                               "data": "joinas:3000:778"}}]
            assert await wait_for(lambda: any(t == "ALERT" and str(p.get("chat_id")) == "3000"
                                              and "Готово, Тимур" in p.get("text", "") for t, m, p in bot.calls))
            assert json.load(open(team, encoding="utf-8"))[0]["kaiten_id"] == 778
            # его заявка — сразу его: бот берёт её и пишет ему
            kaiten.cards[6] = card(6, responsible=778, owner=600, client="@Client_Two")
            assert await wait_for(lambda: (6, {"column_id": IN_WORK}) in kaiten.patches, timeout=15)
            assert await wait_for(lambda: any(t == "ALERT" and str(p.get("chat_id")) == "3000"
                                              and "оплата 6" in p.get("text", "") for t, m, p in bot.calls))
            # его команды — ему
            bot.updates = [{"update_id": 4, "message": {"message_id": 2, "chat": {"id": 3000, "type": "private"},
                                                        "from": {"id": 3000}, "text": "/status"}}]
            assert await wait_for(lambda: any(t == "ALERT" and str(p.get("chat_id")) == "3000"
                                              and p.get("text", "").startswith("✅ Работаю") for t, m, p in bot.calls))
        finally:
            await client.close()
    finally:
        app_main.TEAM_INTERVAL_S = old_team_interval
        await kaiten.close()
        await bot.close()

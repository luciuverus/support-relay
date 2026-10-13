"""v10: пауза и команды, напоминание «актуально ли?», ожидание неизвестного чата, эксплуатация, обновление .env."""
import logging
import shutil
import socket
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.control import Control
from app.main import SecretFilter, in_cloud_folder, port_is_free, startup_notice
from app.setup_wizard import parse_env
from app.tools import upgrade_env
from app.transports import SendError
from tests.fakes import IN_WORK, card, run
from tests.test_relay import env

FOLLOWUP_TEXTS = {"kaiten_comment": ["Взял"], "employee_taken": ["Привет, заявка {card_id}"],
                  "client_taken": ["Клиенту {card_id}"],
                  "followup": ["{greeting}! Проблема по заявке {card_id} ещё актуальна?"]}
AUTHOR = "username:author_person"


def owner_msg(text, chat_id=1000):
    return {"update_id": 1, "message": {"message_id": 1, "text": text,
                                        "chat": {"id": chat_id, "type": "private"}, "from": {"id": chat_id}}}


# ---------------- команды: /status /pause /resume ----------------

@run
async def test_control_status_only_for_owner():
    async with env() as e:
        control = Control(e.relay, None, "1000", connected=lambda: True)
        await control.handle(owner_msg("/status", chat_id=777))  # чужой
        assert e.alert.sent == []
        await control.handle(owner_msg("/status"))
        assert "Работаю с" in e.alert.sent[-1] and "подключён к корпоративному аккаунту: да" in e.alert.sent[-1]
        await control.handle(owner_msg("/что-то"))
        await control.handle(owner_msg("/help"))
        assert "/pause" in e.alert.sent[-1]


@run
async def test_control_pause_stops_everything_and_resume_restores():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        await control.handle(owner_msg("/pause"))
        assert "Пауза" in e.alert.sent[-1]
        e.kaiten.cards[2] = card(2)
        e.clock.now += timedelta(hours=10)  # и сообщения, и SLA уже «пора»
        assert await e.relay.poll_queue() == ["paused:alerted"]   # не берём, но предупреждаем
        assert await e.relay.send_due() == 0 and await e.relay.check_sla() == 0
        assert e.kaiten.cards[2]["column_id"] == 11
        await control.handle(owner_msg("/resume"))
        assert await e.relay.poll_queue() == ["taken"]
        assert await e.relay.send_due() == 2


@run
async def test_control_timed_pause_ends_by_itself():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        await control.handle(owner_msg("/pause 1,5"))
        assert await e.relay.is_paused()
        e.clock.now += timedelta(minutes=91)
        assert not await e.relay.is_paused()
        assert "Пауза закончилась" in e.alert.sent[-1]
        await control.handle(owner_msg("/pause abc"))
        assert "Не понял" in e.alert.sent[-1]
        await control.handle(owner_msg("/pause 999999"))
        assert "30 дней" in e.alert.sent[-1] and not await e.relay.is_paused()


@run
async def test_control_manual_button_cancels_and_strangers_cannot():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        stranger = {"update_id": 2, "callback_query": {"id": "q", "from": {"id": 5}, "data": "manual:1:employee"}}
        await control.handle(stranger)
        assert len(e.storage.pending_group(1, "employee")) == 1
        mine = {"update_id": 3, "callback_query": {"id": "q", "from": {"id": 1000}, "data": "manual:1:employee"}}
        await control.handle(mine)
        assert e.storage.pending_group(1, "employee") == []
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1  # клиенту всё равно уходит
        assert [c.key for c, _ in e.transport.sent] == ["username:client_one"]


# ---------------- «проблема актуальна?» ----------------

async def waiting_env(e):
    e.write_texts(FOLLOWUP_TEXTS)


def wcard(card_id, **kw):
    kw.setdefault("column", 77)
    return card(card_id, **kw)


@run
async def test_followup_once_after_silence_and_never_for_old_cards():
    async with env(WAITING_COLUMN_IDS="77") as e:
        await waiting_env(e)
        e.kaiten.cards[3] = wcard(3)                       # уже ждала ответа до включения
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = wcard(5)                       # перенесли в «Ждём ответа» после
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=5, minutes=59)
        assert "followup:queued" not in await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=2, minutes=2)       # > 8 ч тишины
        assert await e.relay.poll_waiting() == ["followup:queued"]
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1
        assert e.transport.sent[-1][1] == "Добрый вечер! Проблема по заявке 5 ещё актуальна?"
        for _ in range(3):
            e.clock.now += timedelta(hours=10)
            await e.relay.poll_waiting()
            e.clock.now += timedelta(minutes=5)
            assert await e.relay.send_due() == 0           # ровно одно напоминание
        assert len([m for m in e.storage.all_messages() if m["role"] == "followup"]) == 1
        assert all(m["card_id"] != 3 for m in e.storage.all_messages())


@run
async def test_followup_waits_for_silence_after_chat_activity():
    async with env(WAITING_COLUMN_IDS="77") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = wcard(5)
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=5)
        e.storage.remember_chat([AUTHOR], 9, e.clock.now, 1)   # автор ответил
        e.clock.now += timedelta(hours=4)
        assert await e.relay.poll_waiting() == ["followup:chat-active"]
        e.clock.now += timedelta(hours=4, minutes=5)
        assert await e.relay.poll_waiting() == ["followup:queued"]


@run
async def test_followup_cancelled_if_card_left_waiting_or_author_answered():
    async with env(WAITING_COLUMN_IDS="77", HUMAN_DELAY_MIN_S="600", HUMAN_DELAY_MAX_S="600") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = wcard(5)
        e.kaiten.cards[6] = wcard(6, owner=600)
        e.relay.employees["id:600"] = "@second_author"
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=8, minutes=1)
        assert sorted(await e.relay.poll_waiting()) == ["followup:queued", "followup:queued"]
        e.kaiten.cards[5]["column_id"] = IN_WORK                  # ты уже вернул в работу
        await e.relay.on_chat_activity(["username:second_author"], True, False, e.clock.now)  # ответил
        e.clock.now += timedelta(minutes=11)
        assert await e.relay.send_due() == 0
        statuses = {m["card_id"]: m["status"] for m in e.storage.all_messages() if m["role"] == "followup"}
        assert statuses == {5: "cancelled", 6: "cancelled"}


@run
async def test_followup_new_episode_after_card_came_back():
    async with env(WAITING_COLUMN_IDS="77") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = wcard(5)
        await e.relay.poll_waiting()
        e.kaiten.cards[5]["column_id"] = IN_WORK
        await e.relay.poll_waiting()                  # ушла из ожидания — забыли
        e.clock.now += timedelta(hours=10)
        e.kaiten.cards[5]["column_id"] = 77           # снова ждём ответа — отсчёт заново
        assert await e.relay.poll_waiting() == ["followup:wait"]


@run
async def test_followup_finds_waiting_column_by_name():
    async with env() as e:
        await waiting_env(e)
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        assert await e.relay.waiting_column_ids() == [77]
        assert e.storage.get("waiting_column_ids") == "77"


@run
async def test_followup_off_hours_goes_out_next_morning():
    async with env(WAITING_COLUMN_IDS="77", WORK_HOURS="11:30-20:30", WORK_DAYS="1-7") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = wcard(5)                         # 12:00 Бишкек
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=10)                   # 22:00 — нерабочее
        assert await e.relay.poll_waiting() == ["followup:queued"]
        first = min(datetime.fromisoformat(m["send_at"]) for m in e.storage.all_messages())
        local = first.astimezone(e.cfg.tz)
        assert local.date().isoformat() == "2026-09-24" and local.hour == 11


# ---------------- ожидание неизвестного чата ----------------

def unknown(e):
    e.transport.fail[AUTHOR] = SendError("ещё не писал", reason="unknown_chat")


@run
async def test_hold_released_when_person_writes():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        unknown(e)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1                   # клиенту ушло, автору — ждём
        assert [m["status"] for m in e.storage.all_messages() if m["role"] == "employee"] == ["held"]
        hold_alert = next(a for a, b in zip(e.alert.sent, e.alert.sent_buttons) if a.startswith("⏳"))
        buttons = e.alert.sent_buttons[e.alert.sent.index(hold_alert)]
        assert "manual:1:employee" in [b.get("callback_data") for row in buttons for b in row]
        e.transport.fail.clear()
        e.clock.now += timedelta(hours=3)
        await e.relay.on_chat_activity([AUTHOR, "id:9"], True, False, e.clock.now)
        assert await e.relay.send_due() == 0                   # человеческая пауза
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1
        assert e.transport.sent[-1][0].key == AUTHOR


@run
async def test_hold_cancelled_when_you_wrote_yourself():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        unknown(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        assert [m["status"] for m in e.storage.all_messages() if m["role"] == "employee"] == ["cancelled"]
        assert "сам" in e.alert.sent[-1]
        e.transport.fail.clear()
        await e.relay.on_chat_activity([AUTHOR], True, False, e.clock.now)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 0


@run
async def test_hold_manual_reply_cancels_pending_greeting_too():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)  # ответил раньше бота
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1
        assert [c.key for c, _ in e.transport.sent] == ["username:client_one"]


@run
async def test_hold_expires_with_one_alert():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        unknown(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        e.clock.now += timedelta(hours=25)
        await e.relay.send_due()
        await e.relay.send_due()
        assert len([a for a in e.alert.sent if a.startswith("⌛")]) == 1
        assert [m["status"] for m in e.storage.all_messages() if m["role"] == "employee"] == ["failed"]


@run
async def test_hold_disabled_asks_you_immediately():
    async with env(HOLD_UNKNOWN_CHAT_H="0") as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        unknown(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert any(a.startswith("✉️ Не отправил") for a in e.alert.sent)


# ---------------- эксплуатация ----------------

def test_ops_secrets_never_reach_the_log():
    record = logging.LogRecord("x", logging.INFO, "f", 1,
                               "call https://api.telegram.org/bot123456789:AAHfake_token_abcdefghijklmnopqrs/getMe %s",
                               ("kaiten-secret-key",), None)
    SecretFilter(["kaiten-secret-key"]).filter(record)
    text = record.getMessage()
    assert "AAHfake" not in text and "kaiten-secret-key" not in text and text.count("***") == 2


def test_ops_second_copy_is_detected():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert not port_is_free("127.0.0.1", port)
    assert port_is_free("127.0.0.1", port)


@run
async def test_ops_heartbeat_once_a_day_with_numbers():
    async with env(WORK_HOURS="11:30-20:30") as e:
        e.clock.now = datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)   # 11:00 — ещё рано
        assert not await e.relay.heartbeat()
        e.kaiten.cards[1] = card(1)
        e.clock.now = datetime(2026, 9, 23, 5, 31, tzinfo=timezone.utc)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert await e.relay.heartbeat()
        assert "взял заявок 1, написал людям 2" in e.alert.sent[-1]
        assert not await e.relay.heartbeat()
        e.clock.now += timedelta(days=1)
        assert await e.relay.heartbeat()


def test_ops_cloud_folder_warning():
    assert in_cloud_folder(r"C:\Users\Bill\OneDrive\Desktop\support-relay")
    assert in_cloud_folder("/Users/bill/Library/iCloud Drive/support-relay")
    assert not in_cloud_folder(r"C:\support-relay")


@run
async def test_ops_startup_notice_not_spammed_by_restarts():
    async with env() as e:
        await startup_notice(e.relay, e.storage, e.cfg)
        await startup_notice(e.relay, e.storage, e.cfg)
        assert len([a for a in e.alert.sent if "Сервис запущен" in a]) == 1


# ---------------- обновление .env ----------------

def project_with_old_env(**replace) -> Path:
    """Папка с .env в том виде, в каком он лежит у Билла после прошлой версии."""
    root = Path(tempfile.mkdtemp())
    shutil.copy(".env.example", root / ".env.example")
    lines = Path("tests/env_v9.txt").read_text(encoding="utf-8").splitlines()
    for key, value in replace.items():
        lines = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines]
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_upgrade_env_moves_old_schedule_and_adds_new_keys():
    root = project_with_old_env()
    lines = upgrade_env(root)
    env = parse_env((root / ".env").read_text(encoding="utf-8"))
    assert env["WORK_HOURS"] == "11:30-20:30" and env["WORK_DAYS"] == "1-7"
    assert env["FOLLOWUP_AFTER_H"] == "6-8" and env["HOLD_UNKNOWN_CHAT_H"] == "24" and "SPECIALIST_NAME" in env
    assert env["KAITEN_TOKEN"] == "real-token-abc" and env["DRY_RUN"] == "false" and env["QUEUE_COLUMN_IDS"] == "11"
    text = (root / ".env").read_text(encoding="utf-8")
    assert "# Если заявка в «Ждём ответа»" in text and "# Как сервис узнаёт" in text
    assert (root / ".env.bak").exists() and len(lines) == 2
    from app.config import Config
    Config.from_env({**env, "ALERT_CHAT_ID": "1", "BUSINESS_BOT_TOKEN": "1:x", "MY_KAITEN_USER_ID": "1",
                     "IN_WORK_COLUMN_ID": "2", "KAITEN_BASE_URL": "https://x", "KAITEN_CARD_URL_PREFIX": "https://x/"})


def test_upgrade_env_keeps_your_own_schedule():
    root = project_with_old_env(WORK_HOURS="10:00-19:00")
    upgrade_env(root)
    env = parse_env((root / ".env").read_text(encoding="utf-8"))
    assert env["WORK_HOURS"] == "10:00-19:00" and env["WORK_DAYS"] == "1-5"


def test_upgrade_env_is_idempotent():
    root = project_with_old_env()
    upgrade_env(root)
    first = (root / ".env").read_text(encoding="utf-8")
    assert upgrade_env(root) == [] and (root / ".env").read_text(encoding="utf-8") == first


# ---------------- ты ответил сам: как это видит Business-бот ----------------

@run
async def test_hold_business_update_tells_manual_from_bot_messages():
    from tests.fakes import business_connection_update, incoming_message_update
    from tests.test_transports import BizEnv
    async with BizEnv() as e:
        seen = []

        async def on_activity(keys, incoming, manual, at, text=""):
            seen.append((incoming, manual))
        e.t.on_activity = on_activity
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update())                     # написал человек
        out = incoming_message_update(update_id=3)
        out["business_message"]["from"] = {"id": 42}
        out["business_message"]["text"] = "Сейчас посмотрю"
        await e.t.handle_update(out)                                           # ты с телефона
        bot_out = incoming_message_update(update_id=4)
        bot_out["business_message"]["from"] = {"id": 42}
        bot_out["business_message"]["sender_business_bot"] = {"id": 1, "is_bot": True}
        await e.t.handle_update(bot_out)                                       # наш бот
        await e.t.handle_update(incoming_message_update(update_id=5, when=e.clock.now))
        await e.t.send(__import__("app.logic", fromlist=["x"]).normalize_contact("@client_one"), "Номер заявки")
        echo = incoming_message_update(update_id=6)
        echo["business_message"]["from"] = {"id": 42}
        echo["business_message"]["text"] = "Номер заявки"
        await e.t.handle_update(echo)                                          # эхо нашего сообщения
        assert seen == [(True, False), (False, True), (True, False)]
        assert e.storage.find_chat("username:client_one")["last_out_at"] is not None


# ---------------- обновление не затирает твои тексты ----------------

def test_texts_created_from_defaults_once_and_kept():
    import random

    from app.humanize import MessageStore
    root = Path(tempfile.mkdtemp())
    default = root / "messages.default.json"
    shutil.copy("config/messages.default.json", default)
    mine = root / "messages.json"
    store = MessageStore(mine, random.Random(0), default)
    assert mine.exists() and store.book.enabled("followup")
    # у Билла старый файл без раздела followup и со своим текстом — берём его текст и дописываем followup
    mine.write_text('{"kaiten_comment": ["мой текст"], "employee_taken": ["a"], "client_taken": ["b"]}',
                    encoding="utf-8")
    store = MessageStore(mine, random.Random(0), default)
    assert store.book.render("kaiten_comment", {})[0] == ["мой текст"]
    assert store.book.enabled("followup")
    assert "мой текст" in mine.read_text(encoding="utf-8")


# ---------------- «Повторно связаться» (Kaiten переносит туда сам через 6 ч) ----------------

def rcard(card_id, **kw):
    kw.setdefault("column", 79)
    return card(card_id, **kw)


@run
async def test_recontact_column_found_and_waiting_not_watched():
    async with env() as e:
        await waiting_env(e)
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        assert await e.relay.followup_columns() == ([79], "recontact")
        await e.relay.poll_waiting()                        # первый проход — всё, что есть, не трогаем
        e.kaiten.cards[5] = card(5, column=77)              # в «Ждём ответа» хоть 10 часов
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=10)
        assert await e.relay.poll_waiting() == []
        assert not [m for m in e.storage.all_messages() if m["role"] == "followup"]


@run
async def test_recontact_reminds_as_soon_as_card_arrives_and_only_once():
    async with env(RECONTACT_COLUMN_IDS="79", WAITING_COLUMN_IDS="77") as e:
        await waiting_env(e)
        e.kaiten.cards[3] = rcard(3)                        # уже лежала там до обновления
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = rcard(5)                        # Kaiten перенёс сейчас
        assert await e.relay.poll_waiting() == ["followup:queued"]
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1
        assert e.transport.sent[-1][1] == "Добрый день! Проблема по заявке 5 ещё актуальна?"
        for _ in range(3):
            e.clock.now += timedelta(hours=10)
            await e.relay.poll_waiting()
            e.clock.now += timedelta(minutes=5)
            assert await e.relay.send_due() == 0
        assert {m["card_id"] for m in e.storage.all_messages() if m["role"] == "followup"} == {5}


@run
async def test_recontact_waits_if_chat_was_active_recently():
    async with env(RECONTACT_COLUMN_IDS="79") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.storage.remember_chat([AUTHOR], 9, e.clock.now - timedelta(hours=2), 1)  # автор писал 2 ч назад
        e.kaiten.cards[5] = rcard(5)
        assert await e.relay.poll_waiting() == ["followup:chat-active"]
        e.clock.now += timedelta(hours=3, minutes=50)       # прошло < 6 ч тишины
        assert await e.relay.poll_waiting() == ["followup:wait"]
        e.clock.now += timedelta(hours=2, minutes=15)       # > 8 ч тишины
        assert await e.relay.poll_waiting() == ["followup:queued"]


@run
async def test_recontact_cancelled_if_you_moved_the_card_before_sending():
    async with env(RECONTACT_COLUMN_IDS="79", HUMAN_DELAY_MIN_S="600", HUMAN_DELAY_MAX_S="600") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = rcard(5)
        await e.relay.poll_waiting()
        e.kaiten.cards[5]["column_id"] = IN_WORK
        e.clock.now += timedelta(minutes=11)
        assert await e.relay.send_due() == 0
        assert [m["status"] for m in e.storage.all_messages() if m["role"] == "followup"] == ["cancelled"]


@run
async def test_recontact_back_again_is_a_new_episode():
    async with env(RECONTACT_COLUMN_IDS="79") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = rcard(5)
        assert await e.relay.poll_waiting() == ["followup:queued"]
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        e.kaiten.cards[5]["column_id"] = 77                 # ты вернул в «Ждём ответа»
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=9)
        e.kaiten.cards[5]["column_id"] = 79                 # позже Kaiten снова перенёс
        assert await e.relay.poll_waiting() == ["followup:queued"]


def test_followup_shipped_text_uses_assalamu_alaikum():
    import json
    data = json.loads(Path("config/messages.default.json").read_text(encoding="utf-8"))
    assert data["followup"] and all(t.startswith("Ассаламу алейкум!") and "{greeting}" not in t
                                    for t in data["followup"])


# ---------------- регламент ----------------

@run
async def test_regulation_greeting_on_weekend_evening_goes_now_but_followup_waits():
    async with env(WORK_HOURS="11:30-20:30", WORK_DAYS="1-5", RECONTACT_COLUMN_IDS="79") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.clock.now = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)   # суббота 22:00 Бишкек
        e.kaiten.cards[1] = card(1)
        e.kaiten.cards[5] = rcard(5)
        await e.relay.poll_queue()                                  # вне смены сам не берёт…
        assert e.storage.all_messages() == [] or all(m["card_id"] != 1 for m in e.storage.all_messages())
        assert await e.relay.take_now(1) == "Взял в работу"        # …но ты нажал «Взять сейчас»
        await e.relay.poll_waiting()
        e.clock.now += timedelta(minutes=3)
        assert await e.relay.send_due() == 2                       # приветствие взятой заявки — сразу
        follow = [m for m in e.storage.all_messages() if m["role"] == "followup"]
        local = datetime.fromisoformat(follow[0]["send_at"]).astimezone(e.cfg.tz)
        assert local.isoweekday() == 1 and local.hour == 11        # напоминание — в понедельник утром


@run
async def test_regulation_held_greeting_tells_you_to_write_now():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        unknown(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        alert = next(a for a in e.alert.sent if a.startswith("⏳"))
        assert "п. 4.2" in alert and "напиши сейчас" in alert
        assert "Текст:\nУвидел заявку «Не работает оплата 1»" in alert   # чтобы скопировать


@run
async def test_regulation_held_greeting_released_immediately_even_after_hours():
    async with env(WORK_HOURS="11:30-20:30") as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        unknown(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        e.transport.fail.clear()
        e.clock.now = e.clock.now.replace(hour=15)                 # 21:00 Бишкек
        await e.relay.on_chat_activity([AUTHOR], True, False, e.clock.now)
        e.clock.now += timedelta(minutes=3)
        assert await e.relay.send_due() == 1


@run
async def test_regulation_pause_alerts_once_per_assigned_ticket():
    async with env() as e:
        e.relay.pause(None)
        e.kaiten.cards[1] = card(1, asap=True)
        e.kaiten.cards[2] = card(2, responsible=123)               # чужая
        assert await e.relay.poll_queue() == ["paused:alerted"]
        assert await e.relay.poll_queue() == ["paused:already-alerted"]
        paused = [a for a in e.alert.sent if a.startswith("⏸")]
        assert len(paused) == 1 and "5 минут" in paused[0] and "СРОЧНАЯ" in paused[0]
        assert e.kaiten.patches == [] and e.storage.all_messages() == []


@run
async def test_regulation_urgent_ticket_flagged():
    async with env() as e:
        e.kaiten.cards[1] = card(1, asap=True)
        e.kaiten.cards[2] = card(2)
        await e.relay.poll_queue()
        taken = [a for a in e.alert.sent if "L2:" in a]
        assert taken[0].startswith("🔥 СРОЧНАЯ ЗАЯВКА") and "п. 4.4" in taken[0]
        assert not taken[1].startswith("🔥")


# ---------------- регламент 4.5: автоперенос в «В работе» и подсказка «в ожидание» ----------------

async def columns_env(e):
    await waiting_env(e)
    e.kaiten.cards[900] = card(900, column=IN_WORK, responsible=123, owner=999001)   # чтобы найти доску
    await e.relay.waiting_column_ids()


@run
async def test_autowork_moves_waiting_card_when_you_write_and_undo_works():
    async with env() as e:
        await columns_env(e)
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[5] = card(5, column=77)                         # «Ждём ответа»
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)   # ты написал
        assert e.kaiten.cards[5]["column_id"] == IN_WORK
        alert = e.alert.sent[-1]
        assert "Перенёс" in alert and "п. 4.5" in alert
        undo = e.alert.sent_buttons[-1][-1][0]
        assert undo["callback_data"] == "back:5:77"
        await control.handle({"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000},
                                                                 "data": undo["callback_data"]}})
        assert e.kaiten.cards[5]["column_id"] == 77
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)   # пишешь «спасибо» дальше
        assert e.kaiten.cards[5]["column_id"] == 77                    # 10 минут не трогаем
        e.clock.now += timedelta(minutes=11)
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        assert e.kaiten.cards[5]["column_id"] == IN_WORK


@run
async def test_autowork_from_recontact_but_never_from_problem_column():
    async with env() as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=79)                         # «Повторно связаться»
        e.kaiten.cards[6] = card(6, column=78, owner=600, client=None)  # «Проблемные»
        e.relay.employees["id:600"] = "@second_author"
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        await e.relay.on_chat_activity(["username:second_author"], False, True, e.clock.now)
        assert e.kaiten.cards[5]["column_id"] == IN_WORK and e.kaiten.cards[6]["column_id"] == 78


@run
async def test_autowork_ignores_author_messages_and_other_peoples_cards():
    async with env() as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=77)
        e.kaiten.cards[7] = card(7, column=77, responsible=123)        # чужая, тот же автор
        await e.relay.on_chat_activity([AUTHOR], True, False, e.clock.now)   # написал автор
        assert e.kaiten.cards[5]["column_id"] == 77
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)   # написали с общего аккаунта
        assert e.kaiten.cards[5]["column_id"] == 77                     # непонятно, ты или коллега — спрашивает
        who = e.alert.sent_buttons[-1][0][0]["callback_data"]
        assert who.startswith("who:") and who.endswith(":me")
        await Control(e.relay, None, "1000").handle(
            {"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000}, "data": who}})
        assert e.kaiten.cards[5]["column_id"] == IN_WORK and e.kaiten.cards[7]["column_id"] == 77


@run
async def test_autowork_several_cards_asks_which():
    async with env() as e:
        await columns_env(e)
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[5] = card(5, column=77)
        e.kaiten.cards[6] = card(6, column=79)
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        assert e.kaiten.cards[5]["column_id"] == 77 and e.kaiten.cards[6]["column_id"] == 79
        choice = [b[0]["callback_data"] for b in e.alert.sent_buttons[-1]]
        assert sorted(choice) == ["work:5", "work:6"]
        await control.handle({"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000}, "data": "work:6"}})
        assert e.kaiten.cards[6]["column_id"] == IN_WORK and e.kaiten.cards[5]["column_id"] == 77


@run
async def test_autowork_switch_off_and_dry_run():
    async with env(AUTO_TO_WORK="false") as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=77)
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        assert e.kaiten.cards[5]["column_id"] == 77
    async with env(DRY_RUN="true") as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=77)
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        assert e.kaiten.cards[5]["column_id"] == 77 and "[тест" in e.alert.sent[-1]


@run
async def test_waithint_once_per_our_message_and_buttons_work():
    async with env() as e:
        await columns_env(e)
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[5] = card(5, column=IN_WORK)
        e.storage.remember_chat([AUTHOR], 9, e.clock.now - timedelta(minutes=5), 1)       # автор писал
        e.storage.remember_chat([AUTHOR], 9, None, None, last_out_at=e.clock.now)         # ты ответил
        e.clock.now += timedelta(minutes=29)
        assert await e.relay.poll_wait_hints() == []
        e.clock.now += timedelta(minutes=2)
        assert await e.relay.poll_wait_hints() == ["hint:5"]
        assert await e.relay.poll_wait_hints() == []                    # один раз
        assert "Ждём ответа" in e.alert.sent[-1]
        keep = {"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000}, "data": "keep:5"}}
        await control.handle(keep)
        e.clock.now += timedelta(hours=2)
        assert await e.relay.poll_wait_hints() == []                    # «Я ещё работаю» — молчим
        e.storage.remember_chat([AUTHOR], 9, None, None, last_out_at=e.clock.now)          # новое сообщение
        e.clock.now += timedelta(minutes=31)
        assert await e.relay.poll_wait_hints() == ["hint:5"]
        wait = {"update_id": 10, "callback_query": {"id": "q", "from": {"id": 1000}, "data": "wait:5"}}
        await control.handle(wait)
        assert e.kaiten.cards[5]["column_id"] == 77


@run
async def test_waithint_silent_when_author_answered_last_or_nobody_wrote():
    async with env() as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=IN_WORK)
        e.clock.now += timedelta(hours=3)
        assert await e.relay.poll_wait_hints() == []                    # никто не писал
        e.storage.remember_chat([AUTHOR], 9, None, None, last_out_at=e.clock.now - timedelta(hours=1))
        e.storage.remember_chat([AUTHOR], 9, e.clock.now - timedelta(minutes=40), 1)     # автор ответил позже
        assert await e.relay.poll_wait_hints() == []                    # мяч у тебя


@run
async def test_waithint_counts_bot_greeting_as_our_message():
    async with env() as e:
        await columns_env(e)
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=3)
        await e.relay.send_due()                                         # приветствие ушло
        e.clock.now += timedelta(minutes=31)
        assert await e.relay.poll_wait_hints() == ["hint:1"]


@run
async def test_autowork_other_peoples_card_even_if_list_has_no_members():
    async with env() as e:
        await columns_env(e)
        e.kaiten.list_without_members = True
        e.kaiten.cards[7] = card(7, column=77, responsible=123)
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now)
        assert e.kaiten.cards[7]["column_id"] == 77
        # второй рубеж: даже если чужую карточку попросили перенести кнопкой — не переносим
        control = Control(e.relay, None, "1000")
        await control.handle({"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000}, "data": "work:7"}})
        assert e.kaiten.cards[7]["column_id"] == 77


# ---------------- карточка без доступа (403) ----------------

@run
async def test_forbidden_card_is_closed_for_sla_not_retried_forever():
    from aiohttp import web
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        calls = []

        async def forbidden(request):
            calls.append(1)
            return web.json_response({"message": "Forbidden"}, status=403)
        for route in e.kaiten.server.app.router.routes():
            if route.method == "GET" and str(route.resource.canonical).endswith("/cards/{id}"):
                route._handler = forbidden  # noqa: SLF001
        e.clock.now += timedelta(hours=10)
        await e.relay.check_sla()
        assert e.storage.open_cards() == []            # закрыли, а не «не смог проверить»
        n = len(calls)
        await e.relay.check_sla()
        assert len(calls) == n                         # больше не дёргаем Kaiten
        assert not any("SLA" in a for a in e.alert.sent[1:])

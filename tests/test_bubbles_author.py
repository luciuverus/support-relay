"""Несколько «пузырей» подряд, автор по e-mail из Service Desk, помощники настройки."""
import json
import random
from datetime import timedelta

from app.humanize import MessageBook
from app.logic import card_emails, find_author, normalize_people
from app.storage import Storage
from app.tools import chats_from_storage, chats_from_userbot, describe_card
from app.transports import SendError
from tests.fakes import MY_ID, card, make_cfg, run
from tests.test_relay import env

SD_EMAIL = "curator@example.com"


def sd_card(card_id=70951934, **extra):
    """Похоже на карточку Service Desk со скриншота: автор — e-mail, в участниках — поддержка."""
    data = card(card_id, owner=1, client=None)
    data["title"] = "Нужна помощь чтоб зайти в ММ с телефона"
    data["members"] = [
        {"id": MY_ID, "type": 2, "email": "ulugbek@example.com", "full_name": "Улугбек Маткаримов"},
        {"id": 999, "type": 1, "email": "maria@example.com"},
    ]
    data["service_desk_request"] = {"author": {"full_name": "Анна Иванова", "email": SD_EMAIL.upper()}}
    data["description"] = "Пишите на maria@example.com если что"
    data.update(extra)
    return data


def use_shipped_texts(e):
    with open("config/messages.default.json", encoding="utf-8") as fh:
        e.write_texts(json.load(fh))


# ---------- пузыри ----------

def test_bubbles_shipped_texts_match_bills_own_messages():
    book = MessageBook.load("config/messages.default.json", random.Random(0))
    values = {"title": "T", "card_id": 70951934, "deadline": "d", "deadline_time": "t",
              "deadline_full": "f", "greeting": "g", "name": "Улугбек"}
    bubbles, _ = book.render("employee_taken", values)
    assert len(bubbles) == 3
    assert bubbles[0].startswith("Ас-саляму алейкум") and bubbles[0].endswith("Номер заявки:")
    assert bubbles[1] == "70951934"
    assert bubbles[2] == "На связи технический специалист - Улугбек"
    assert book.render("kaiten_comment", values)[0] == ["напишу в тг"]


@run
async def test_bubbles_sent_in_order_after_comment():
    async with env() as e:
        use_shipped_texts(e)
        e.relay.employees = normalize_people({SD_EMAIL: "@anna_curator"})
        e.kaiten.cards[70951934] = sd_card()
        assert await e.relay.handle_webhook({"data": {"id": 70951934}}) == "taken"
        assert e.kaiten.comments == [(70951934, "напишу в тг")]
        rows = e.storage.all_messages()
        assert [r["text"] for r in rows][1] == "70951934" and len(rows) == 3
        times = [r["send_at"] for r in rows]
        assert times == sorted(times) and len(set(times)) == 3  # строго по порядку
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 3
        sent = [text for _, text in e.transport.sent]
        assert sent[1] == "70951934" and sent[2].endswith("Улугбек") and sent[0].startswith("Ас-саляму")
        assert all(c.key == "username:anna_curator" for c, _ in e.transport.sent)


@run
async def test_bubbles_failure_stops_whole_group_and_alerts_once():
    async with env() as e:
        use_shipped_texts(e)
        e.relay.employees = normalize_people({SD_EMAIL: "@anna_curator"})
        e.kaiten.cards[70951934] = sd_card()
        await e.relay.handle_webhook({"data": {"id": 70951934}})
        e.transport.fail["username:anna_curator"] = SendError("не писала больше 24 часов")
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 0
        assert {r["status"] for r in e.storage.all_messages()} == {"failed"}
        alerts = [a for a in e.alert.sent if "Не отправил" in a]
        assert len(alerts) == 1 and "70951934" in alerts[0] and "Улугбек" in alerts[0]


@run
async def test_bubbles_flood_wait_keeps_order():
    async with env() as e:
        use_shipped_texts(e)
        e.relay.employees = normalize_people({SD_EMAIL: "@anna_curator"})
        e.kaiten.cards[70951934] = sd_card()
        await e.relay.handle_webhook({"data": {"id": 70951934}})
        e.transport.fail["username:anna_curator"] = SendError("flood", retry_after_s=30)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 0
        e.transport.fail.clear()
        e.clock.now += timedelta(minutes=2)
        assert await e.relay.send_due() == 3
        assert e.transport.sent[1][1] == "70951934"


# ---------- автор по e-mail ----------

def test_author_card_emails_skip_support_staff_and_prefer_request_fields():
    emails = card_emails(sd_card())
    assert emails[0] == ("service_desk_request.author.email", SD_EMAIL)
    assert all(email not in {"ulugbek@example.com", "maria@example.com"} for _, email in emails)


def test_author_matched_by_email_not_by_staff():
    people = normalize_people({"maria@example.com": "@maria", SD_EMAIL.title(): "@anna_curator", "_c": "x"})
    how, contact, _ = find_author(sd_card(), people, MY_ID)
    assert (how, contact) == (SD_EMAIL, "@anna_curator")
    how, contact, emails = find_author(sd_card(), normalize_people({"maria@example.com": "@maria"}), MY_ID)
    assert contact is None and emails == [SD_EMAIL]


def test_author_fallback_to_kaiten_id_and_never_me():
    people = normalize_people({"500": "@author_person", str(MY_ID): "@me"})
    assert find_author(card(1, owner=500, client=None), people, MY_ID)[1] == "@author_person"
    assert find_author(card(1, owner=MY_ID, client=None), people, MY_ID)[1] is None


@run
async def test_author_unknown_email_is_named_in_alert():
    async with env() as e:
        e.relay.employees = {}
        e.kaiten.cards[70951934] = sd_card()
        await e.relay.handle_webhook({"data": {"id": 70951934}})
        assert SD_EMAIL in e.alert.sent[0] and "employees.json" in e.alert.sent[0]


# ---------- помощники настройки ----------

@run
async def test_helper_cli_chats_from_business_storage():
    storage = Storage(":memory:")
    assert "Пока никого" in (await chats_from_storage(storage))[0]
    from datetime import datetime, timezone
    storage.remember_chat(["id:500112345", "username:anna_curator"], 500112345,
                          datetime(2026, 9, 23, 12, 49, tzinfo=timezone.utc), 5, "Анна Куратор")
    storage.remember_chat(["id:600000001"], 600000001, None, None, "Мария")
    lines = await chats_from_storage(storage)
    assert lines[0] == "Анна Куратор — @anna_curator — chat id 500112345, писал 23.09 12:49 UTC"
    assert lines[1] == "Мария — (нет @username) — chat id 600000001"


@run
async def test_helper_cli_chats_from_userbot():
    class Entity:
        def __init__(self, id_, username):
            self.id, self.username = id_, username

    class Dialog:
        def __init__(self, name, entity, is_user=True):
            self.name, self.entity, self.is_user = name, entity, is_user

    class Client:
        async def iter_dialogs(self, limit):
            for d in [Dialog("Анна Куратор", Entity(500112345, None)),
                      Dialog("Группа поддержки", Entity(-100, None), is_user=False)]:
                yield d

    lines = await chats_from_userbot(Client())
    assert lines == ["Анна Куратор — (нет @username) — chat id 500112345"]


@run
async def test_helper_cli_describe_card_shows_author_email_and_match():
    async with env() as e:
        e.kaiten.cards[70951934] = sd_card(column=11)
        lines = await describe_card(e.cfg, e.relay.kaiten, {}, 70951934)
        text = "\n".join(lines)
        assert "«Очередь»" in text and "Ты ответственный: да" in text
        assert SD_EMAIL in text and "service_desk_request.author.email" in text
        assert "maria@example.com" not in text and "Автору напишем: никому" in text
        lines = await describe_card(e.cfg, e.relay.kaiten, normalize_people({SD_EMAIL: "500112345"}), 70951934)
        assert f"Автору напишем: 500112345 (взял из: employees.json ({SD_EMAIL}))" in lines
        assert "не найдена" in (await describe_card(e.cfg, e.relay.kaiten, {}, 1))[0]


def test_helper_cli_config_accepts_shipped_env_shape():
    assert make_cfg().queue_column_ids


@run
async def test_helper_cli_business_updates_record_names():
    from tests.test_transports import BizEnv
    from tests.fakes import incoming_message_update
    async with BizEnv() as e:
        update = incoming_message_update(username=None)
        update["business_message"]["chat"].update(first_name="Анна", last_name="Куратор")
        update["business_message"]["chat"].pop("username")
        await e.t.handle_update(update)
        lines = await chats_from_storage(e.storage)
        assert lines[0].startswith("Анна Куратор — (нет @username) — chat id 500112345")

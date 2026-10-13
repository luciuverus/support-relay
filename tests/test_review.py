"""Четыре замечания из ревью: отмена во время «печатает…», серии сообщений, «Готово» при пустой
«В работе», выбор заявки после «оцените работу»."""
import asyncio
from datetime import timedelta

from app.control import Control
from app.logic import normalize_contact
from app.storage import Storage
from app.transports import SendCancelled
from tests.fakes import IN_WORK, NEW_COL, card, incoming_message_update, run
from tests.test_relay import env
from tests.test_transports import BizEnv, business_connection_update
from tests.test_v10 import owner_msg

KEYS = ["id:4242", "username:author_person"]
WAITING, DONE, CAT, SOL = 77, 90, "id_45", "id_46"
AUTHOR = "username:author_person"


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


def honest_send(e, before_send):
    """Транспорт, который, как настоящий, спрашивает still_wanted() перед отправкой."""
    original = e.transport.send

    async def send(contact, text, still_wanted=None):
        await before_send(contact, text)
        await asyncio.sleep(0)
        if still_wanted and not still_wanted():
            raise SendCancelled("отменено")
        await original(contact, text)
    e.transport.send = send


# ---------------- 1. отмена во время «печатает…» ----------------

@run
async def test_review_cancel_button_while_typing_stops_message():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        pressed = []

        async def press(contact, text):
            if contact.key == AUTHOR and not pressed:
                pressed.append(await e.relay.mark_manual(1, "employee"))
        honest_send(e, press)
        await e.relay.send_due()
        assert pressed == [1]
        assert [c.key for c, _ in e.transport.sent] == ["username:client_one"]      # клиенту ушло, сотруднику нет
        statuses = {m["role"]: m["status"] for m in e.storage.all_messages()}
        assert statuses == {"employee": "cancelled", "client": "sent"}              # «cancelled» не затёрт


@run
async def test_review_manual_reply_mid_series_stops_remaining_bubbles():
    async with env() as e:
        e.write_texts({"kaiten_comment": ["Взял"], "employee_taken": [["Раз {card_id}", "Два", "Три"]],
                       "client_taken": []})
        e.kaiten.cards[1] = card(1, client=None)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        calls = []

        async def cancel_after_first(contact, text):
            calls.append(text)
            if len(calls) == 2:  # первый ушёл, второй «печатается» — ты написал сам
                e.storage.fail_group(1, "employee", "написал вручную", "cancelled")
        honest_send(e, cancel_after_first)
        await e.relay.send_due()
        assert [t for _, t in e.transport.sent] == ["Раз 1"]
        assert [m["status"] for m in e.storage.all_messages()] == ["sent", "cancelled", "cancelled"]


@run
async def test_review_cancel_arriving_during_network_call_stops_the_rest():
    async with env() as e:
        e.write_texts({"kaiten_comment": ["Взял"], "employee_taken": [["Раз {card_id}", "Два", "Три"]],
                       "client_taken": []})
        e.kaiten.cards[1] = card(1, client=None)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        original = e.transport.send

        async def send(contact, text, still_wanted=None):  # транспорт без проверки — страхует send_due
            await original(contact, text)          # Telegram уже принял первое…
            if len(e.transport.sent) == 1:          # …и в ту же секунду ты нажал «Отправил вручную»
                await e.relay.mark_manual(1, "employee")
        e.transport.send = send
        await e.relay.send_due()
        assert [t for _, t in e.transport.sent] == ["Раз 1"]
        assert [m["status"] for m in e.storage.all_messages()] == ["sent", "cancelled", "cancelled"]


@run
async def test_review_business_transport_checks_before_sending():
    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update(when=e.clock.now - timedelta(minutes=1)))
        contact = normalize_contact("@Client_One")
        wanted = iter([True, False, False, False, False, False])
        try:
            await e.t.send(contact, "Длинное сообщение, которое печатается несколько секунд подряд",
                           still_wanted=lambda: next(wanted))
        except SendCancelled:
            pass
        else:
            raise AssertionError("not cancelled")
        assert "sendMessage" not in e.bot.methods("BIZ")
        await e.t.send(contact, "Короткое", still_wanted=lambda: True)
        assert "sendMessage" in e.bot.methods("BIZ")


# ---------------- 2. каждая серия сообщений — отдельно ----------------

async def _greet_then_retake(e):
    e.kaiten.cards[1] = card(1)
    await e.relay.poll_queue()
    e.clock.now += timedelta(minutes=5)
    await e.relay.send_due()
    first = len(e.transport.sent)
    assert first == 2
    e.kaiten.cards[1]["column_id"] = NEW_COL
    e.clock.now += timedelta(hours=13)
    assert await e.relay.poll_queue() == ["taken"]
    return first


@run
async def test_review_retaken_card_reassigned_gets_nothing():
    async with env(REGREET_AFTER_H="12") as e:
        first = await _greet_then_retake(e)
        e.kaiten.cards[1]["members"] = [{"id": 999, "type": 2}]
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert len(e.transport.sent) == first


@run
async def test_review_retaken_card_manual_reply_cancels_new_series():
    async with env(REGREET_AFTER_H="12") as e:
        first = await _greet_then_retake(e)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Здравствуйте")
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert [c.key for c, _ in e.transport.sent[first:]] == ["username:client_one"]  # автору — нет


@run
async def test_review_retaken_card_still_mine_is_greeted_again():
    async with env(REGREET_AFTER_H="12") as e:
        first = await _greet_then_retake(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert len(e.transport.sent) == first + 2


def test_review_series_are_independent_and_old_rows_count_as_old():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    s = Storage(":memory:")
    old = s.enqueue(1, "followup", "@a", "раньше", now, now)            # строка старой версии: без серии
    s.set_message_status(old, "sent", at=now)
    assert s.group_started(1, "followup") is False                      # сейчас ничего не ждёт
    s.enqueue(1, "followup", "@a", "новое", now, now, "s2")
    assert s.current_series(1, "followup") == "s2"
    assert s.group_started(1, "followup") is False                      # старая отправка не считается
    assert s.group_started(1, "followup", "") is True


# ---------------- 3. «Готово» при пустой «В работе» ----------------

@run
async def test_review_close_from_waiting_with_empty_in_work():
    async with env() as e:
        e.kaiten.cards[5] = card(5, column=IN_WORK, responsible=999, owner=600)
        await e.relay.waiting_column_ids()
        del e.kaiten.cards[5]
        e.kaiten.cards[1] = card(1, column=WAITING, properties={CAT: [501], SOL: "сделал", "id_42": "@x"})
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Оцените работу")
        assert e.kaiten.cards[1]["column_id"] == DONE and "Закрыл" in e.alert.sent[-1]


@run
async def test_review_noreply_from_waiting_with_empty_in_work():
    async with env() as e:
        e.kaiten.cards[5] = card(5, column=IN_WORK, responsible=999, owner=600)
        await e.relay.waiting_column_ids()
        del e.kaiten.cards[5]
        e.kaiten.cards[1] = card(1, column=WAITING)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now,
                                       text="Мы не получили от Вас ответа, поэтому вынуждены закрыть заявку.")
        assert e.kaiten.cards[1]["column_id"] == DONE


# ---------------- 4. выбор из нескольких заявок после «оцените работу» ----------------

@run
async def test_review_choice_after_rate_closes_filled_card():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=IN_WORK, properties={CAT: [501], SOL: "сделал", "id_42": "@x"})
        e.kaiten.cards[2] = card(2, column=IN_WORK)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Оцените работу")
        buttons = [b["callback_data"] for row in e.alert.sent_buttons[-1] for b in row]
        assert buttons == ["fill:1:1", "fill:2:1"]
        await control.handle(cb("fill:1:1"))
        assert e.kaiten.cards[1]["column_id"] == DONE and e.kaiten.cards[2]["column_id"] == IN_WORK


@run
async def test_review_choice_after_rate_closes_after_filling():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        e.kaiten.cards[2] = card(2, column=IN_WORK)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Оцените работу")
        await control.handle(cb("fill:2:1"))
        await control.handle(cb("fcat:2:501"))
        await control.handle(owner_msg("сделал"))
        assert e.kaiten.cards[2]["column_id"] == DONE and e.kaiten.cards[1]["column_id"] == IN_WORK


@run
async def test_review_choice_after_close_question_does_not_close():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        e.kaiten.cards[2] = card(2, column=IN_WORK)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Закрываем заявку?")
        assert [b["callback_data"] for row in e.alert.sent_buttons[-1] for b in row] == ["fill:1:0", "fill:2:0"]
        await control.handle(cb("fill:1:0"))
        await control.handle(cb("fcat:1:501"))
        await control.handle(owner_msg("сделал"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK            # «закрываем?» ещё не закрывает


@run
async def test_review_second_followup_series_cancelled_by_manual_reply():
    from tests.test_v10 import rcard, waiting_env
    async with env(RECONTACT_COLUMN_IDS="79", WAITING_COLUMN_IDS="77") as e:
        await waiting_env(e)
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = rcard(5)
        assert await e.relay.poll_waiting() == ["followup:queued"]
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1                 # первое «актуально ли?» ушло
        e.kaiten.cards[5]["column_id"] = IN_WORK             # взял в работу…
        await e.relay.poll_waiting()
        e.clock.now += timedelta(hours=9)
        e.kaiten.cards[5]["column_id"] = 79                  # …и снова «Повторно связаться»
        assert await e.relay.poll_waiting() == ["followup:queued"]
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Как дела с заявкой?")
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 0                 # ты уже написал сам — второе не уходит



@run
async def test_review_followup_alert_says_clearly_nothing_sent_yet():
    from tests.test_v10 import rcard, waiting_env
    async with env(RECONTACT_COLUMN_IDS="79", WAITING_COLUMN_IDS="77", WORK_HOURS="11:30-20:30") as e:
        await waiting_env(e)
        e.clock.now = e.clock.now.replace(hour=17, minute=50)        # 23:50 в Бишкеке
        await e.relay.poll_waiting()
        e.kaiten.cards[5] = rcard(5)
        assert await e.relay.poll_waiting() == ["followup:queued"]
        text = e.alert.sent[-1]
        assert "пока НИЧЕГО не отправил" in text and "когда начнётся рабочий день" in text
        assert e.transport.sent == []
        e.clock.now += timedelta(hours=1)
        assert await e.relay.send_due() == 0                          # ночью не уходит



@run
async def test_review_prefill_button_when_bot_cannot_write():
    from urllib.parse import parse_qs, urlparse
    from app.transports import SendError
    async with env() as e:
        e.write_texts({"kaiten_comment": ["Взял"], "employee_taken": [["Ассаламу алейкум! Заявка #{card_id} & 😊", "+ на связи"]],
                       "client_taken": ["Клиенту {card_id}"]})
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        e.transport.fail["username:author_person"] = SendError("не писал", reason="unknown_chat")
        e.transport.fail["username:client_one"] = SendError("сломалось")
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        urls = [b["url"] for rows in e.alert.sent_buttons if rows for row in rows for b in row if "?text=" in b.get("url", "")]
        held = next(u for u in urls if "author_person" in u)
        failed = next(u for u in urls if "client_one" in u)
        texts = {}
        for m in e.storage.all_messages():
            texts[m["role"]] = (texts[m["role"]] + "\n" + m["text"]) if m["role"] in texts else m["text"]
        assert parse_qs(urlparse(held).query)["text"][0] == texts["employee"]   # весь текст, закодирован верно
        assert " " not in held and "\n" not in held and "#" not in held.split("?", 1)[1]
        assert parse_qs(urlparse(failed).query)["text"][0] == texts["client"]
        assert held.startswith("https://t.me/author_person?text=")
        held_alert = next(a for a in e.alert.sent if a.startswith("⏳"))
        assert texts["employee"] in held_alert                                  # и в самом сообщении для копирования


@run
async def test_review_prefill_falls_back_to_text_for_phone_contacts():
    from app.transports import SendError
    async with env() as e:
        e.kaiten.cards[1] = card(1, client="+79311070513")
        await e.relay.poll_queue()
        e.transport.fail["phone:+79311070513"] = SendError("по номеру нельзя")
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        alert = next(a for a in e.alert.sent if a.startswith("✉️"))
        assert "Напиши вручную" in alert
        assert not any("79311070513?text" in b.get("url", "") for rows in e.alert.sent_buttons if rows
                       for row in rows for b in row)


@run
async def test_review_prefill_two_taps_no_manual_button_needed():
    from app.transports import SendError
    async with env() as e:
        e.kaiten.cards[1] = card(1, client=None)
        await e.relay.poll_queue()
        e.transport.fail["username:author_person"] = SendError("не писал", reason="unknown_chat")
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert e.storage.groups_with_status("held")                       # бот ждёт
        held_alert = next(a for a in e.alert.sent if a.startswith("⏳"))
        assert "нажми «✍️ Открыть чат с готовым текстом»" in held_alert and "больше ничего не нужно" in held_alert
        assert "переключись на корпоративный аккаунт" in held_alert   # без чата аккаунта — предупреждаем
        # ты нажал кнопку и «Отправить» — бот видит твоё сообщение в этом чате
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Ассаламу алейкум!")
        assert not e.storage.groups_with_status("held")                   # отметил сам, без кнопки
        # человек ответил — бот НЕ дублирует приветствие
        del e.transport.fail["username:author_person"]
        await e.relay.on_chat_activity(KEYS, incoming=True, manual=False, at=e.clock.now)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 0 and e.transport.sent == []

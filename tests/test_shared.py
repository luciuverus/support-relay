"""Общий корпоративный аккаунт: сообщение коллеги не должно трогать твои заявки."""
from datetime import timedelta

from app.control import Control
from tests.fakes import IN_WORK, card, run
from tests.test_relay import env

KEYS = ["id:4242", "username:author_person"]
WAITING, DONE, CAT, SOL = 77, 90, "id_45", "id_46"
COLLEAGUE = {"id": 888, "type": 2, "full_name": "Сидорова Мария"}


def colleague_card(card_id=7, column=IN_WORK):
    return card(card_id, column=column, members=[COLLEAGUE])


def cb(data):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000}, "data": data}}


async def say(e, text):
    await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text=text)


def who_buttons(e):
    return [b["callback_data"] for rows in e.alert.sent_buttons if rows for row in rows for b in row
            if str(b.get("callback_data", "")).startswith("who:")]


@run
async def test_shared_colleague_greeting_with_name_does_not_move_my_card():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=WAITING)
        e.kaiten.cards[7] = colleague_card()
        await say(e, "Ас-саляму алейкум! Номер заявки: 7 На связи технический специалист - Мария")
        assert e.kaiten.cards[1]["column_id"] == WAITING and who_buttons(e) == []


@run
async def test_shared_colleague_ticket_number_ignored_mine_acts():
    async with env() as e:
        e.kaiten.cards[11111111] = card(11111111, column=WAITING)
        e.kaiten.cards[22222222] = colleague_card(22222222)
        await say(e, "Подскажите по заявке 22222222")
        assert e.kaiten.cards[11111111]["column_id"] == WAITING and who_buttons(e) == []   # даже не спрашивает
        await say(e, "По заявке 11111111 всё проверил")
        assert e.kaiten.cards[11111111]["column_id"] == IN_WORK and who_buttons(e) == []


@run
async def test_shared_my_name_acts():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=WAITING)
        e.kaiten.cards[7] = colleague_card()
        await say(e, "На связи технический специалист - Улугбек")
        assert e.kaiten.cards[1]["column_id"] == IN_WORK and who_buttons(e) == []


@run
async def test_shared_unclear_asks_then_remembers_for_30_min():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=WAITING)
        e.kaiten.cards[7] = colleague_card()
        await say(e, "Добрый день, посмотрю")
        assert e.kaiten.cards[1]["column_id"] == WAITING
        ask = e.alert.sent[-1]
        assert "Это ты написал?" in ask and "№1" in ask and "№7 (Мария)" in ask
        me = next(b for b in who_buttons(e) if b.endswith(":me"))
        await control.handle(cb(me))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK
        await control.handle(cb(me))                                   # повторное нажатие — ничего
        e.kaiten.cards[1]["column_id"] = WAITING
        before = len(who_buttons(e))
        await say(e, "Ещё вопрос")                                      # в течение 30 минут — без вопроса
        assert e.kaiten.cards[1]["column_id"] == IN_WORK and len(who_buttons(e)) == before
        e.clock.now += timedelta(minutes=31)
        e.kaiten.cards[1]["column_id"] = WAITING
        await say(e, "И ещё")
        assert e.kaiten.cards[1]["column_id"] == WAITING and len(who_buttons(e)) == before + 2


@run
async def test_shared_answer_colleague_ignores_and_remembers():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=WAITING)
        e.kaiten.cards[7] = colleague_card()
        await say(e, "Добрый день")
        other = next(b for b in who_buttons(e) if b.endswith(":other"))
        await control.handle(cb(other))
        assert e.kaiten.cards[1]["column_id"] == WAITING
        before = len(e.alert.sent)
        await say(e, "Ещё вопрос")
        assert e.kaiten.cards[1]["column_id"] == WAITING and len(e.alert.sent) == before


@run
async def test_shared_colleague_rating_does_not_close_my_card():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK, properties={CAT: [501], SOL: "сделал", "id_42": "@x"})
        e.kaiten.cards[7] = colleague_card()
        await say(e, "Пожалуйста оцените качество работы специалиста по заявке по 5-балльной шкале 😊")
        assert e.kaiten.cards[1]["column_id"] == IN_WORK                # не закрыл — сначала спросил
        assert who_buttons(e)
        await Control(e.relay, None, "1000").handle(cb(next(b for b in who_buttons(e) if b.endswith(":me"))))
        assert e.kaiten.cards[1]["column_id"] == DONE                   # это был ты — закрыл


@run
async def test_shared_colleague_message_keeps_my_pending_greeting():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        e.kaiten.cards[7] = colleague_card()
        await say(e, "Номер заявки: 7, на связи Мария")
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 2                           # твоё приветствие ушло, не отменено


@run
async def test_shared_no_colleague_ticket_behaves_as_before():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=WAITING)
        e.kaiten.cards[7] = card(7, column=IN_WORK, members=[COLLEAGUE], owner=600)   # другой автор
        await say(e, "Добрый день")
        assert e.kaiten.cards[1]["column_id"] == IN_WORK and who_buttons(e) == []

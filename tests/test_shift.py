"""Вне смены бот заявки сам не берёт: одно уведомление с «Взять сейчас», в начале смены — берёт сам."""
from datetime import datetime, timedelta, timezone

from app.control import Control
from tests.fakes import IN_WORK, card, run
from tests.test_relay import env
from tests.test_speed import cb

MORNING = datetime(2026, 10, 2, 4, 15, tzinfo=timezone.utc)   # 10:15 в Бишкеке, смена с 11:30


@run
async def test_shift_card_before_shift_is_not_taken_until_start():
    async with env(WORK_HOURS="11:30-20:30") as e:
        e.clock.now = MORNING
        e.kaiten.cards[1] = card(1)
        assert await e.relay.poll_queue() == ["offshift:alerted"]
        assert e.kaiten.patches == [] and e.kaiten.comments == [] and e.storage.all_messages() == []
        alert = e.alert.sent[-1]
        assert "11:30" in alert and "«Не работает оплата 1»" in alert
        assert e.alert.sent_buttons[-1][0][0] == {"text": "▶️ Взять сейчас", "callback_data": "take:1"}
        e.clock.now += timedelta(minutes=30)
        assert await e.relay.poll_queue() == ["offshift:already-alerted"]   # не спамит каждые 30 с
        assert len([a for a in e.alert.sent if a.startswith("🌙")]) == 1
        assert "назначили вне смены" in e.relay.journal_text(1)
        e.clock.now = MORNING.replace(hour=5, minute=31)                      # 11:31 — смена началась
        assert await e.relay.poll_queue() == ["taken"]
        assert e.kaiten.patches == [(1, {"column_id": IN_WORK})]


@run
async def test_shift_take_now_button_takes_at_once_owner_only():
    async with env(WORK_HOURS="11:30-20:30") as e:
        e.clock.now = MORNING
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        control = Control(e.relay, None, "1000")
        await control.handle(cb("take:1", sender=555))                       # чужой — ничего
        assert e.kaiten.patches == []
        await control.handle(cb("take:1"))
        assert e.kaiten.patches == [(1, {"column_id": IN_WORK})]
        assert len(e.storage.all_messages()) == 2                             # приветствия поставлены
        assert await e.relay.take_now(1) == "Уже в работе"


@run
async def test_shift_after_end_and_webhook_also_wait():
    async with env(WORK_HOURS="11:30-20:30") as e:
        e.clock.now = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)      # 21:00 — смена кончилась
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "offshift:alerted"
        assert e.kaiten.patches == []
        e.kaiten.cards[2] = card(2, responsible=12345)                        # чужая — молчим
        assert await e.relay.handle_webhook({"data": {"id": 2}}) == "offshift:not-mine"


@run
async def test_shift_take_outside_hours_keeps_old_behaviour():
    async with env(WORK_HOURS="11:30-20:30", TAKE_OUTSIDE_HOURS="true") as e:
        e.clock.now = MORNING
        e.kaiten.cards[1] = card(1)
        assert await e.relay.poll_queue() == ["taken"]


EVENING = datetime(2026, 10, 2, 15, 11, tzinfo=timezone.utc)   # 21:11 в Бишкеке — смена кончилась


@run
async def test_shift_duty_messages_off_shift_do_not_move_or_close_your_cards():
    from tests.test_v10 import AUTHOR, columns_env
    async with env(WORK_HOURS="11:30-20:30") as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=77)                                # твоя, в «Ждём ответа»
        e.clock.now = EVENING
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now, text="Добрый вечер, проверим")
        assert e.kaiten.cards[5]["column_id"] == 77                           # дежурный написал — не трогаем
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now,
                                       text="Не получили от вас ответа, вынуждены закрыть заявку")
        assert e.kaiten.cards[5]["column_id"] == 77                           # и не закрываем
        assert not any(a.startswith("🤔") for a in e.alert.sent)              # и не дёргаем «это ты?»
        e.clock.now = EVENING.replace(hour=6)                                 # 12:11 — твоя смена
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now, text="Добрый день")
        assert e.kaiten.cards[5]["column_id"] == IN_WORK


@run
async def test_shift_after_take_now_your_messages_count_for_three_hours():
    from tests.test_v10 import AUTHOR, columns_env
    async with env(WORK_HOURS="11:30-20:30") as e:
        await columns_env(e)
        e.clock.now = MORNING
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        assert await e.relay.take_now(1) == "Взял в работу"                  # ты пришёл раньше
        e.kaiten.cards[5] = card(5, column=77)
        e.clock.now += timedelta(minutes=20)
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now, text="Здравствуйте")
        assert e.kaiten.cards[5]["column_id"] == IN_WORK


@run
async def test_shift_pause_messages_from_account_do_not_touch_your_cards():
    from tests.test_v10 import AUTHOR, columns_env
    async with env() as e:
        await columns_env(e)
        e.kaiten.cards[5] = card(5, column=77)
        e.relay.pause(None)                                                   # /pause — ты не работаешь
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now, text="Добрый день")
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now,
                                       text="Не получили от вас ответа, вынуждены закрыть заявку")
        assert e.kaiten.cards[5]["column_id"] == 77
        assert not any(a.startswith("🤔") for a in e.alert.sent)
        e.relay.resume()
        await e.relay.on_chat_activity([AUTHOR], False, True, e.clock.now, text="Добрый день")
        assert e.kaiten.cards[5]["column_id"] == IN_WORK

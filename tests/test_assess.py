"""REACTION_MODE=assess: время реакции = когда специалист реально взял заявку в работу (созвон 09.10.2026)."""
from datetime import timedelta

import pytest

from app.assess import eta_phrase
from app.config import ConfigError
from app.control import COMMANDS, HELP, HELP_ALL, Control
from tests.fakes import IN_WORK, NEW_COL, card, make_cfg, run
from tests.test_relay import env

INFO = 88          # «Ожидание информации для оценки»
WAITING = 77
DONE = 90
CAT = "id_45"
CLIENT = ["id:5151", "username:client_one"]
AUTHOR = ["id:4242", "username:author_person"]
ASSESS = {"REACTION_MODE": "assess", "ASSESS_ETA_MIN": "Камера/Микрофон=60; *=40"}


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


def msg(text, chat=1000):
    return {"update_id": 5, "message": {"chat": {"id": chat, "type": "private"}, "text": text}}


def buttons(e, index=-1):
    rows = e.alert.sent_buttons[index] or []
    return [b.get("callback_data") for row in rows for b in row]


def with_info_column(e):
    e.kaiten.board["columns"].insert(2, {"id": INFO, "title": "Ожидание информации для оценки"})


async def greet(e, card_id=1, **extra):
    e.kaiten.cards[card_id] = card(card_id, **extra)
    assert await e.relay.handle_webhook({"data": {"id": card_id}}) == "greeted"


@run
async def test_assess_greets_but_card_stays_new_and_is_not_retaken():
    async with env(**ASSESS) as e:
        await greet(e)
        assert e.kaiten.patches == []                            # в «В работе» сам не переносит
        assert e.kaiten.cards[1]["column_id"] == NEW_COL
        assert len(e.kaiten.comments) == 1                       # «напишу в тг» в карточке — как раньше
        assert "останется в «Новой»" in e.alert.sent[-1]
        assert {"go:1", "info:1"} <= set(buttons(e))
        assert "поздоровался, жду оценку" in e.relay.journal_text(1)
        # приветствие уходит, хотя карточка в «Новой»
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 2
        # следующий опрос «Очереди» её не трогает и второй раз не здоровается
        assert await e.relay.poll_queue() == ["ignored:assessing"]
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "ignored:assessing"
        assert len(e.transport.sent) == 2 and e.kaiten.patches == []
        # SLA работы ещё не начался: проверка срока карточку не закрывает
        e.clock.now += timedelta(hours=5)
        assert await e.relay.check_sla() == 0
        assert e.storage.open_cards() and e.relay.assess.state(1)


@run
async def test_classic_mode_is_unchanged_by_default():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert e.kaiten.patches == [(1, {"column_id": IN_WORK})]
        assert e.relay.assess.state(1) is None
        await Control(e.relay, None, "1000").handle(msg("/work 1"))
        assert "REACTION_MODE=classic" in e.alert.sent[-1]


@run
async def test_work_needs_category_then_moves_and_sends_eta_by_category():
    async with env(**ASSESS) as e:
        await greet(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        control = Control(e.relay, None, "1000")
        await control.handle(cb("go:1"))
        assert e.kaiten.cards[1]["column_id"] == NEW_COL         # категории нет — не переносим
        assert "Фактическая категория" in e.alert.sent[-1] and "go:1" in buttons(e)
        e.kaiten.cards[1]["properties"][CAT] = [504]             # «Камера/Микрофон»
        e.clock.now += timedelta(minutes=2)
        await control.handle(cb("go:1"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK
        assert e.relay.assess.state(1) is None
        assert "1 час" in e.alert.sent[-1] and "клиенту" in e.alert.sent[-1]
        row = next(r for r in e.storage.open_cards() if r["card_id"] == 1)
        assert row["deadline"] == e.clock.now + timedelta(minutes=60)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 1
        contact, text = e.transport.sent[-1]
        assert contact.key == "username:client_one" and "1 час" in text and "1" in text
        assert "в работе по команде" in e.relay.journal_text(1)
        # SLA работы теперь идёт: напоминание на 80 % срока
        e.clock.now += timedelta(minutes=45)
        assert await e.relay.check_sla() == 1


@run
async def test_work_default_eta_and_command_without_number():
    async with env(**ASSESS) as e:
        await greet(e)
        e.kaiten.cards[1]["properties"][CAT] = [501]             # Bitrix24 — срока в таблице нет, берём 40
        await Control(e.relay, None, "1000").handle(msg("/work"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK
        assert "40 минут" in e.alert.sent[-1]


@run
async def test_work_command_lists_cards_when_several_wait():
    async with env(**ASSESS) as e:
        await greet(e, 1)
        await greet(e, 2, owner=600, client="@Other_Client")
        await Control(e.relay, None, "1000").handle(msg("/work"))
        assert set(buttons(e)) == {"go:1", "go:2"}
        await Control(e.relay, None, "1000").handle(msg("/вработу 2"))
        assert e.kaiten.cards[2]["column_id"] == NEW_COL         # категория пустая
        assert "№2" in e.alert.sent[-1] and "Фактическая категория" in e.alert.sent[-1]
        await Control(e.relay, None, "1000").handle(msg("/work 99"))
        assert e.alert.sent[-1] == "№99: Заявка уже не на тебе"


@run
async def test_require_category_off_and_unknown_fields_still_work():
    async with env(**ASSESS, ASSESS_REQUIRE_CATEGORY="false") as e:
        await greet(e)
        assert await e.relay.assess.start_work(1) == "В работе"
        assert e.kaiten.cards[1]["column_id"] == IN_WORK
    async with env(**ASSESS) as e:
        e.kaiten.props = [p for p in e.kaiten.props if p["id"] not in (45, 46)]
        await greet(e)
        assert await e.relay.assess.start_work(1) == "В работе"
        assert "не проверял" in e.alert.sent[-1]


@run
async def test_wait_for_info_pauses_and_client_answer_returns_to_new():
    async with env(**ASSESS) as e:
        with_info_column(e)
        await greet(e)
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        # быстрый ответ «ждём ответа», пока заявку не брали в работу → ждём информацию для оценки
        await e.relay.on_chat_activity(CLIENT, incoming=False, manual=True, at=e.clock.now,
                                       text="Ожидаем вашего ответа 🙏")
        assert e.kaiten.cards[1]["column_id"] == INFO
        assert "Ожидание информации для оценки" in e.alert.sent[-1]
        assert await e.relay.assess.poll() == ["assess:info-wait:1"]
        # пишет кто-то другой — не трогаем
        await e.relay.on_chat_activity(["id:1", "username:someone_else"], incoming=True, manual=False,
                                       at=e.clock.now, text="привет")
        assert e.kaiten.cards[1]["column_id"] == INFO
        # клиент ответил → обратно в «Новую», время реакции снова идёт
        await e.relay.on_chat_activity(CLIENT, incoming=True, manual=False, at=e.clock.now, text="вот скрин")
        assert e.kaiten.cards[1]["column_id"] == NEW_COL
        assert "ответили — вернул в «Новую»" in e.alert.sent[-1] and "go:1" in buttons(e)
        assert "вернул в «Новую» — клиент ответил" in e.relay.journal_text(1)
        assert await e.relay.poll_queue() == ["ignored:assessing"]


@run
async def test_info_button_and_missing_column():
    async with env(**ASSESS) as e:
        await greet(e)
        control = Control(e.relay, None, "1000")
        await control.handle(cb("info:1"))
        assert e.kaiten.cards[1]["column_id"] == NEW_COL
        assert "нет колонки «Ожидание информации для оценки»" in e.alert.sent[-1]
        with_info_column(e)
        e.relay.assess._wait_lookup_at = None
        await control.handle(cb("info:1"))
        assert e.kaiten.cards[1]["column_id"] == INFO
        # из ожидания информации тоже можно сразу взять в работу
        e.kaiten.cards[1]["properties"][CAT] = [502]
        await control.handle(cb("go:1"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK


@run
async def test_explicit_info_column_id():
    async with env(**ASSESS, ASSESS_WAIT_COLUMN_IDS=str(INFO)) as e:
        await greet(e)
        assert await e.relay.assess.to_info_wait(1)
        assert e.kaiten.cards[1]["column_id"] == INFO


@run
async def test_reminder_once_after_threshold_and_quiet_on_pause():
    async with env(**ASSESS) as e:
        await greet(e)
        assert await e.relay.assess.poll() == ["assess:new:1"]
        e.clock.now += timedelta(minutes=4)
        e.relay.pause(1)
        assert await e.relay.assess.poll() == ["assess:new:1"]   # на паузе не напоминаем
        e.relay.resume()
        assert await e.relay.assess.poll() == ["assess:reminded:1"]
        assert "в «Новой» уже 4 мин" in e.alert.sent[-1]
        assert await e.relay.assess.poll() == ["assess:new:1"]   # один раз


@run
async def test_manual_move_in_kaiten_offers_eta_and_cleanup():
    async with env(**ASSESS) as e:
        await greet(e)
        e.kaiten.cards[1]["column_id"] = IN_WORK                 # перенёс сам в Kaiten
        assert await e.relay.assess.poll() == ["assess:manual-work:1"]
        assert e.relay.assess.state(1) is None and "eta:1" in buttons(e)
        await Control(e.relay, None, "1000").handle(cb("eta:1"))
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert any("Ориентировочное время решения" in t for _, t in e.transport.sent)
        # карточку закрыли или забрали — состояние убираем
        await greet(e, 2, owner=600, client="@Other_Client")
        e.kaiten.cards[2]["column_id"] = DONE
        assert await e.relay.assess.poll() == ["assess:gone:2"]
        await greet(e, 3, owner=601, client="@Third")
        e.kaiten.cards[3]["members"] = [{"id": 999, "type": 2}]
        assert await e.relay.assess.poll() == ["assess:not-mine:3"]
        assert e.relay.assess.waiting_cards() == []


@run
async def test_reopened_after_work_is_greeted_as_new_assessment():
    async with env(**ASSESS) as e:
        await greet(e)
        e.kaiten.cards[1]["properties"][CAT] = [502]
        assert await e.relay.assess.start_work(1) == "В работе"
        e.kaiten.cards[1]["column_id"] = NEW_COL                 # вернули в «Очередь»
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "greeted"
        assert e.relay.assess.state(1)


@run
async def test_off_shift_card_waiting_assessment_is_not_alerted_as_new():
    async with env(**ASSESS, WORK_HOURS="11:30-20:30") as e:
        await greet(e)
        e.clock.now = e.clock.now.replace(hour=18)              # 00:00 в Бишкеке
        assert await e.relay.poll_queue() == ["ignored:assessing"]
        e.clock.now += timedelta(minutes=30)
        assert await e.relay.assess.poll() == ["assess:new:1"]   # ночью не напоминаем


@run
async def test_fields_in_bot_off_only_reminds():
    async with env(FIELDS_IN_BOT="false") as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await e.relay.on_chat_activity(AUTHOR, incoming=False, manual=True, at=e.clock.now,
                                       text="Можно закрывать заявку?")
        assert "заполни в Kaiten" in e.alert.sent[-1]
        assert not any(b and b.startswith("fcat:") for b in buttons(e))


def test_help_is_short_and_debug_commands_moved():
    assert "/log" not in HELP and "/log" in HELP_ALL and "/work" in HELP
    names = {c["command"] for c in COMMANDS}
    assert "log" not in names and "work" in names and len(names) <= 8


def test_eta_phrase_and_config_errors():
    assert [eta_phrase(m) for m in (1, 2, 5, 21, 40, 60, 90, 120, 300)] == [
        "1 минута", "2 минуты", "5 минут", "21 минута", "40 минут", "1 час", "1 час 30 минут", "2 часа", "5 часов"]
    assert make_cfg().reaction_mode == "classic"
    assert make_cfg(REACTION_MODE="оценка").reaction_mode == "assess"
    cfg = make_cfg(ASSESS_ETA_MIN="Камера/Микрофон=60; Bitrix24=120; *=45")
    assert cfg.assess_eta_default_min == 45 and dict(cfg.assess_eta_by_category)["bitrix24"] == 120
    for bad in ({"REACTION_MODE": "auto"}, {"ASSESS_ETA_MIN": "сорок"}, {"ASSESS_ETA_MIN": "0"}):
        with pytest.raises(ConfigError):
            make_cfg(**bad)

"""Журнал действий: что бот сделал с заявкой — /log, /log <номер>."""
import os
import tempfile
from datetime import timedelta

from app.control import Control
from app.storage import Storage
from tests.fakes import IN_WORK, card, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

KEYS = ["id:4242", "username:author_person"]
DONE, CAT, SOL = 90, "id_45", "id_46"


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


def actions(e, card_id=None):
    return [r["action"] for r in e.storage.journal(card_id, limit=200)]


@run
async def test_journal_ticket_lifecycle_and_log_commands():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.write_texts({"kaiten_comment": ["Взял"], "employee_taken": [["Раз {card_id}", "Два", "Три"]],
                       "client_taken": ["Клиенту {card_id}"]})
        e.kaiten.cards[1] = card(1, asap=True)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        got = actions(e, 1)
        assert got[0] == "взял в работу (срочная)"
        assert "написал автору (@author_person)" in got and "написал клиенту (@Client_One)" in got
        assert len([a for a in got if a.startswith("написал автору")]) == 1      # одна строка на серию, не на «пузырь»
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Ждем от вас обратную связь")
        assert actions(e, 1)[-1] == "перенёс в «Ждём ответа»"
        await control.handle(owner_msg("/log 1"))
        text = e.alert.sent[-1]
        assert text.startswith("📜 История заявки №1:") and "взял в работу" in text and "Ждём ответа" in text
        await control.handle(owner_msg("/log"))
        assert e.alert.sent[-1].startswith("📜 Последние действия бота:") and "№1 — взял в работу" in e.alert.sent[-1]
        await control.handle(owner_msg("/log 999"))
        assert "записей нет" in e.alert.sent[-1]
        before = len(e.alert.sent)
        await control.handle(owner_msg("/log", chat_id=555))
        assert len(e.alert.sent) == before


@run
async def test_journal_quick_close_and_reopen_recorded():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now, text="Оцените работу")
        await control.handle(cb("fcat:1:501"))
        await control.handle(owner_msg("сменили метод оплаты"))
        await control.handle(cb(f"freopen:1:{IN_WORK}"))
        got = actions(e, 1)
        assert got == ["записал категорию «Bitrix24»", "записал «что сделали»: сменили метод оплаты",
                       "закрыл после «оцените работу»", "вернул из «Готово» по кнопке"]


@run
async def test_journal_noreply_manual_cancel_and_reassign_recorded():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await e.relay.on_chat_activity(KEYS, incoming=False, manual=True, at=e.clock.now,
                                       text="Мы не получили от Вас ответа, поэтому вынуждены закрыть заявку.")
        assert actions(e, 1) == ["закрыл: клиент не ответил (категория «Решилось само/неактуально»)"]
    async with env() as e:
        e.kaiten.cards[2] = card(2)
        await e.relay.poll_queue()
        await e.relay.mark_manual(2, "employee")
        e.kaiten.cards[2]["members"] = [{"id": 999, "type": 2}]
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        got = actions(e, 2)
        assert "ты отметил «Отправил вручную» — сообщение автору не шлю" in got
        assert "не стал писать клиенту: заявку переназначили или перенесли" in got


@run
async def test_journal_sla_and_dry_run_marked():
    async with env(DRY_RUN="true", SLA_MINUTES="60") as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        assert actions(e, 1)[0] == "[тест] взял бы в работу"
    async with env(SLA_MINUTES="60") as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=50)
        await e.relay.check_sla()
        assert any(a.startswith("до SLA") for a in actions(e, 1))


def test_journal_survives_restart_and_is_pruned_after_90_days():
    from datetime import datetime, timezone
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "state.db")
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    s = Storage(path)
    s.journal_add(now - timedelta(days=100), 1, "старое")
    s.journal_add(now, 1, "новое")
    s.close()
    s2 = Storage(path)
    assert [r["action"] for r in s2.journal(1)] == ["старое", "новое"]
    s2.journal_prune(now - timedelta(days=90))
    assert [r["action"] for r in s2.journal(1)] == ["новое"]


@run
async def test_journal_prune_runs_once_a_day():
    async with env() as e:
        e.storage.journal_add(e.clock.now - timedelta(days=95), 1, "старое")
        assert e.relay.prune_journal() is True and actions(e) == []
        e.storage.journal_add(e.clock.now - timedelta(days=95), 1, "старое2")
        assert e.relay.prune_journal() is False and actions(e) == ["старое2"]
        e.clock.now += timedelta(days=1, minutes=1)
        assert e.relay.prune_journal() is True and actions(e) == []

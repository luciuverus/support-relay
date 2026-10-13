"""Бот на команду: случаи, найденные при независимой проверке."""
import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

from app.storage import Storage
from app.team import load_team
from app.tools import set_mode
from tests.fakes import card, run
from tests.test_team import COLLEAGUE, TeamEnv, buttons, cb, msg, two_cards
from tests.test_windows_live import project_with_env


def test_go_live_wipes_colleagues_test_state_too():
    root = project_with_env()
    now = datetime.now(timezone.utc)
    mate = Storage(str(root / "data" / f"state-{COLLEAGUE}.db"))
    mate.claim_card(2, "t", "u", now, now, now)
    mate.set("greeted:2:username:client_two", now.isoformat())
    mate.set("assess:2", '{"since": "x"}')
    mate.close()
    set_mode(root, live=True)
    mate = Storage(str(root / "data" / f"state-{COLLEAGUE}.db"))
    assert not mate.has_card(2) and mate.get("greeted:2:username:client_two") is None and mate.get("assess:2") is None
    mate.close()
    shutil.rmtree(root)


def test_bad_or_repeated_team_entries_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "team.json"
    path.write_text(json.dumps([
        {"chat_id": 2000, "kaiten_id": 778, "name": "Тимур", "work_hours": "9-18"},     # плохая смена
        {"chat_id": 2001, "kaiten_id": 779, "name": "Мария"},
        {"chat_id": 2002, "kaiten_id": 779, "name": "Повтор"},                          # тот же Kaiten
        {"chat_id": 1000, "kaiten_id": 780, "name": "Чат главного"},                     # чат главного
        {"chat_id": 2003, "kaiten_id": 777, "name": "Сам главный"},                      # id главного
        {"name": "без id"},
    ]), encoding="utf-8")
    problems = []
    members = load_team(str(path), problems, 777, "1000")
    assert [m.name for m in members] == ["Мария"] and len(problems) == 5
    path.write_text("{ не json", encoding="utf-8")
    problems = []
    assert load_team(str(path), problems) == [] and "ошибка JSON" in problems[0]


@run
async def test_rejoin_after_removal_and_reprompt_after_ten_minutes():
    async with TeamEnv() as t:
        e = t.e
        e.storage.team_seen(5, title="t", lane="", topic="", created_at=e.clock.now, entered_at=e.clock.now,
                            now=e.clock.now, responsible_id=COLLEAGUE, responsible="Тимур Тестов",
                            in_queue=False, urgent=False)
        e.storage.set("mgr_asked:3000", "1")             # старая отметка: когда-то уже писал /start
        await t.team.handle(msg("/start", chat="3000"))
        assert "join:3000" in buttons(e.alert)
        count = len(e.alert.sent)
        await t.team.handle(msg("/start", chat="3000"))
        assert len(e.alert.sent) == count                 # не спамим
        e.clock.now += timedelta(minutes=11)
        await t.team.handle(msg("/start", chat="3000"))
        assert len(e.alert.sent) == count + 1
        await t.team.handle(cb("join:3000"))
        await t.team.handle(cb("joinas:3000:778"))
        member, relay, _ = t.team.member_by_chat("3000")
        await t.team.handle(msg("/start", chat="3000"))   # уже в команде — это не «чужой»
        assert len(e.alert.sent) == count + 3
        await t.team.handle(cb("unjoin:778"))
        assert "больше не работает на тебя" in relay.alert.sent[-1]
        await t.team.handle(msg("/start", chat="3000"))   # убрали — может снова попроситься сразу
        assert "join:3000" in buttons(e.alert)


@run
async def test_config_warnings_go_only_to_primary():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        e.write_texts("{ сломанный json")
        await mate.texts()
        assert not any("messages.json" in x for x in mate.alert.sent)
        await e.relay.texts()
        assert any("messages.json" in x for x in e.alert.sent)
        e.kaiten.props = []
        assert await mate.field_key("Нет такого поля") is None
        assert not any("Не нашёл в Kaiten поле" in x for x in mate.alert.sent)


@run
async def test_pasted_key_is_never_written_anywhere():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        mate.storage.set("await_solution", "2")
        await t.team.handle(msg("abcDEF1234567890ghijKLMN5678", message_id=7))
        assert "Похоже на ключ" in mate.alert.sent[-1]
        assert mate.storage.get("await_solution") == "2" and not e.kaiten.patches
        assert "abcDEF1234567890" not in mate.journal_text()


@run
async def test_name_command_changes_greeting_name():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague(name="Тестов")
        await t.team.handle(msg("/name Тимур"))
        assert mate.name == "Тимур" and load_team(t.team_file)[0].name == "Тимур"
        assert "Тимур" in mate.alert.sent[-1]


@run
async def test_colleague_field_dialog_kept_while_card_is_on_the_board():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        e.storage.team_seen(9, title="t", lane="", topic="", created_at=e.clock.now, entered_at=e.clock.now,
                            now=e.clock.now, responsible_id=COLLEAGUE, responsible="Тимур",
                            in_queue=False, urgent=False)
        mate.storage.set("fill:9", '{"asked": true}')
        mate.storage.set("fill:10", '{"asked": true}')
        mate.storage.prune_old(e.clock.now - timedelta(days=60), e.clock.now - timedelta(days=90),
                               e.clock.now - timedelta(days=1))
        assert mate.storage.get("fill:9") and mate.storage.get("fill:10") is None


@run
async def test_column_listing_is_shared_briefly_and_refreshed_after_a_move():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        e.relay.kaiten.list_ttl_s = 5
        two_cards(e)
        before = len(e.kaiten.list_calls)
        await e.relay.kaiten.list_cards(11)
        await mate.kaiten.list_cards(11)
        assert len(e.kaiten.list_calls) == before + 1          # один запрос на двоих
        await e.relay.kaiten.move_card(1, 55)
        rows = await mate.kaiten.list_cards(11)
        assert len(e.kaiten.list_calls) == before + 2 and [r["id"] for r in rows] == [2]


@run
async def test_two_specialists_writing_to_one_chat_do_not_interleave():
    from tests.fakes import business_connection_update, incoming_message_update
    from tests.test_transports import BizEnv
    from app.logic import normalize_contact

    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        await e.t.handle_update(incoming_message_update(when=e.clock.now - timedelta(hours=1)))

        async def slow(_):
            await asyncio.sleep(0.01)
        e.t.sleep = slow
        e.t.typing_time = lambda text: 9.0                    # два «печатает…» по 4.5 с
        contact = normalize_contact("@client_one")
        await asyncio.gather(e.t.send(contact, "первое"), e.t.send(contact, "второе"))
        seq = [(m, p.get("text")) for _, m, p in e.bot.calls if m in ("sendChatAction", "sendMessage")]
        first = seq.index(("sendMessage", "первое"))
        assert all(m == "sendChatAction" for m, _ in seq[:first]) and seq[first + 1][0] == "sendChatAction"
        assert seq[-1] == ("sendMessage", "второе") and len([1 for m, _ in seq[:first] if m == "sendChatAction"]) == 2


@run
async def test_removed_colleague_db_is_closed_and_pending_messages_reported():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        e.kaiten.cards[2] = card(2, responsible=COLLEAGUE, owner=600, client="@Client_Two")
        await mate.poll_queue()
        assert await t.team.remove(COLLEAGUE)
        assert "Неотправленных сообщений: 1" in mate.alert.sent[-1]
        try:
            mate.storage.get("x")
            closed = False
        except Exception:  # noqa: BLE001
            closed = True
        assert closed and os.path.exists(t.team_file)


@run
async def test_primary_name_written_into_texts_is_reported_when_colleague_joins():
    async with TeamEnv() as t:
        e = t.e
        texts = e.read_texts()
        texts["client_taken"] = ["На связи технический специалист - Улугбек, заявка {card_id}"]
        e.write_texts(texts)
        await e.relay.texts()
        e.storage.team_seen(5, title="t", lane="", topic="", created_at=e.clock.now, entered_at=e.clock.now,
                            now=e.clock.now, responsible_id=COLLEAGUE, responsible="Руслан Петров",
                            in_queue=False, urgent=False)
        await t.team.handle(msg("/start", chat="3000"))
        await t.team.handle(cb("join:3000"))
        await t.team.handle(cb("joinas:3000:778"))
        assert any("имя «Улугбек» написано текстом" in x and "client_taken" in x for x in e.alert.sent)
        from app.team import hardcoded_name
        texts["client_taken"] = ["На связи технический специалист - {name}"]
        e.write_texts(texts)
        assert hardcoded_name((await e.relay.texts()), "Улугбек") == []

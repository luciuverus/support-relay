"""Бот на всю команду: несколько специалистов на одном корпоративном аккаунте."""
import json
import os
import tempfile
from datetime import timedelta

from app.control import Control
from app.team import Member, MemberStorage, Team, build_member, load_team, parse_shift, shift_text
from tests.fakes import IN_WORK, MY_ID, card, run
from tests.test_relay import env

COLLEAGUE = 778
WAITING = 77


def msg(text, chat="2000", message_id=None):
    m = {"chat": {"id": int(chat), "type": "private"}, "text": text, "from": {"id": int(chat), "first_name": "Тимур"}}
    if message_id:
        m["message_id"] = message_id
    return {"update_id": 5, "message": m}


def cb(data, sender="1000"):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": int(sender)}, "data": data}}


def buttons(alert, index=-1):
    rows = alert.sent_buttons[index] or []
    return [b.get("callback_data") for row in rows for b in row]


class TeamEnv:
    """Главный (из env) + коллега 778 с чатом 2000."""

    def __init__(self, **overrides):
        self.tmp = tempfile.mkdtemp()
        self.team_file = os.path.join(self.tmp, "team.json")
        self.env = env(TEAM_FILE=self.team_file, **overrides)

    async def __aenter__(self):
        e = await self.env.__aenter__()
        self.e = e
        self.control = Control(e.relay, None, "1000")
        self.team = Team(e.cfg, e.relay, self.control)
        self.control.team = self.team

        async def spawn(member):
            return build_member(member, cfg=e.cfg, primary=e.relay, transport=e.transport, team=self.team)
        self.team.spawn = spawn
        return self

    async def add_colleague(self, name="Тимур", kaiten_id=COLLEAGUE, chat="2000"):
        member = Member(chat_id=chat, kaiten_id=kaiten_id, name=name)
        relay, control = await self.team.spawn(member)
        self.team.members.append((member, relay, control))
        return relay

    async def __aexit__(self, *exc):
        await self.env.__aexit__(*exc)


def two_cards(e):
    e.kaiten.cards[1] = card(1)                                                   # заявка главного
    e.kaiten.cards[2] = card(2, responsible=COLLEAGUE, owner=600, client="@Client_Two")  # заявка коллеги


@run
async def test_each_specialist_takes_only_own_ticket_and_greets_with_own_name():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        texts = e.read_texts()
        texts["client_taken"] = ["На связи технический специалист {name}, заявка {card_id}"]
        e.write_texts(texts)
        two_cards(e)
        assert await e.relay.poll_queue() == ["taken"]
        assert await mate.poll_queue() == ["taken"]
        assert e.kaiten.cards[1]["column_id"] == IN_WORK and e.kaiten.cards[2]["column_id"] == IN_WORK
        assert "Не работает оплата 1" in e.alert.sent[-1] and "Не работает оплата 2" in mate.alert.sent[-1]
        assert not any("оплата 2" in t for t in e.alert.sent)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 2 and await mate.send_due() == 1    # у коллеги нет автора в employees
        sent = {c.key: text for c, text in e.transport.sent}
        assert "Тимур" in " ".join(text for c, text in e.transport.sent if c.key == "username:client_two")
        assert "Улугбек" in " ".join(text for c, text in e.transport.sent if c.key == "username:client_one")
        assert sent  # оба писали с одного корпоративного аккаунта
        # отдельные базы: у главного — только его заявка
        assert [r["card_id"] for r in e.storage.open_cards()] == [1]
        assert [r["card_id"] for r in mate.storage.open_cards()] == [2]


@run
async def test_quick_reply_from_shared_account_moves_only_that_specialists_card():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        two_cards(e)
        e.kaiten.cards[1]["column_id"] = IN_WORK
        e.kaiten.cards[2]["column_id"] = IN_WORK
        await t.team.on_chat_activity(["id:9", "username:client_two"], incoming=False, manual=True,
                                      at=e.clock.now, text="Ожидаем вашего ответа")
        assert e.kaiten.cards[2]["column_id"] == WAITING and e.kaiten.cards[1]["column_id"] == IN_WORK
        assert "Ждём ответа" in mate.alert.sent[-1]
        assert not any("Ждём ответа" in x for x in e.alert.sent)


@run
async def test_chats_and_shared_settings_live_in_primary_storage():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        assert isinstance(mate.storage, MemberStorage)
        e.storage.remember_chat(["username:client_two"], 5151, e.clock.now, 1)
        assert mate.storage.find_chat("username:client_two")["chat_id"] == 5151
        mate.set_speed("instant")
        assert e.storage.get("speed") == "instant" and e.relay.pace().key == "instant"
        mate.pause(1)
        assert mate.storage.get("paused_until") and not e.storage.get("paused_until")   # пауза — своя


@run
async def test_commands_route_by_chat_and_colleague_cannot_stop_the_service():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        await t.team.handle(msg("/status"))
        assert mate.alert.sent and mate.alert.sent[-1].startswith("✅ Работаю") and not e.alert.sent
        await t.team.handle(msg("/stop"))
        assert "главный специалист" in mate.alert.sent[-1]
        await t.team.handle(msg("/people"))
        assert "главный специалист" in mate.alert.sent[-1]
        await t.team.handle(msg("/status", chat="1000"))
        assert e.alert.sent[-1].startswith("✅ Работаю")


@run
async def test_shift_command_changes_and_saves_colleague_shift():
    async with TeamEnv(WORK_HOURS="11:30-20:30") as t:
        e = t.e
        mate = await t.add_colleague()
        await t.team.handle(msg("/shift 9:00-18:00 1-5"))
        assert shift_text(mate.cfg) == "09:00–18:00, пн–пт" and "✅" in mate.alert.sent[-1]
        assert e.cfg.work_start.hour == 11                                         # у главного не поменялось
        saved = load_team(t.team_file)
        assert saved[0].work_hours == "09:00-18:00" and saved[0].work_days == "1-5"
        await t.team.handle(msg("/shift утром"))
        assert "Не понял" in mate.alert.sent[-1]


@run
async def test_join_flow_adds_colleague_without_restart():
    async with TeamEnv() as t:
        e = t.e
        e.storage.team_seen(5, title="t", lane="", topic="", created_at=e.clock.now, entered_at=e.clock.now,
                            now=e.clock.now, responsible_id=COLLEAGUE, responsible="Тимур Тестов",
                            in_queue=False, urgent=False)
        await t.team.handle(msg("/start", chat="3000"))
        assert "join:3000" in buttons(e.alert)
        await t.team.handle(cb("join:3000", sender="3001"))                       # чужой нажал — ничего
        assert "joinas:3000:778" not in buttons(e.alert)
        await t.team.handle(cb("join:3000"))
        assert buttons(e.alert) == ["joinas:3000:778"]
        await t.team.handle(cb("joinas:3000:778"))
        assert "Добавил Тимур Тестов" in e.alert.sent[-1]
        member, relay, _ = t.team.member_by_chat("3000")
        assert member.name == "Тимур" and relay.cfg.my_user_id == COLLEAGUE
        assert "Готово, Тимур" in relay.alert.sent[-1]
        assert json.load(open(t.team_file, encoding="utf-8"))[0]["kaiten_id"] == COLLEAGUE
        # сразу работает: берёт свою заявку
        e.kaiten.cards[2] = card(2, responsible=COLLEAGUE, owner=600, client="@Client_Two")
        assert await relay.poll_queue() == ["taken"]
        # второй раз того же не добавить; /people показывает и умеет убрать
        assert t.team.candidates() == []
        await t.team.handle(msg("/people", chat="1000"))
        assert "Тимур" in e.alert.sent[-1] and "unjoin:778" in buttons(e.alert)
        await t.team.handle(cb("unjoin:778"))
        assert t.team.members == [] and load_team(t.team_file) == []


@run
async def test_report_counts_whole_team_and_lesson_checks_counted_once():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        two_cards(e)
        await e.relay.poll_queue()
        await mate.poll_queue()
        from app.reports import Reporter
        reporter = Reporter(e.relay, None, team=t.team)
        today = e.clock.now.astimezone(e.cfg.tz).date()
        text = reporter.text(reporter.period("week", today, previous=False))
        assert "взял заявок в работу: 2" in text
        report = "Отчёт\n#lesson-check 1.4 problems=cam.perm left="
        await t.team.on_chat_activity(["id:9", "username:client_two"], incoming=True, manual=False,
                                      at=e.clock.now, text=report)
        rows = e.storage._exec("SELECT COUNT(*) AS n FROM lesson_checks")[0]["n"]
        mine = mate.storage._exec("SELECT COUNT(*) AS n FROM lesson_checks")[0]["n"]
        assert rows == 1 and mine == 0


@run
async def test_token_command_checks_owner_and_switches_kaiten_client():
    async with TeamEnv() as t:
        e = t.e
        mate = await t.add_colleague()
        e.kaiten.users_by_token = {"colleague-key-1": COLLEAGUE, "someone-else-9": 999}
        await t.team.handle(msg("/token someone-else-9", message_id=11))
        assert "другого пользователя" in mate.alert.sent[-1]
        await t.team.handle(msg("/token colleague-key-1", message_id=12))
        assert "Ключ подключён" in mate.alert.sent[-1]
        assert mate.kaiten is not e.relay.kaiten and mate.quick.kaiten is mate.kaiten
        assert load_team(t.team_file)[0].kaiten_token == "colleague-key-1"


@run
async def test_webhook_goes_to_the_right_specialist():
    async with TeamEnv() as t:
        e = t.e
        await t.add_colleague()
        two_cards(e)
        assert await t.team.handle_webhook({"data": {"id": 2}}) == ["ignored:not-mine", "taken"]


def test_member_file_and_shift_parsing():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "team.json")
        assert load_team(path) == []
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"chat_id": 2000, "kaiten_id": "778", "name": "Тимур", "work_hours": "14:00-23:00"}], fh)
        member = load_team(path)[0]
        assert member.chat_id == "2000" and member.kaiten_id == 778 and member.work_hours == "14:00-23:00"
    assert parse_shift("9:00–18:00") == ("09:00-18:00", "")
    assert parse_shift("14:00-23:00 1-7") == ("14:00-23:00", "1-7")
    assert MY_ID != COLLEAGUE

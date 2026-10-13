"""Статистика проверок перед уроком: служебная строка #lesson-check в комментариях Kaiten и в чатах."""
from datetime import date, datetime, timedelta, timezone

from app.control import Control
from app.lessonstats import SCAN_BATCH, label, parse, PC_LABELS
from app.reports import Reporter
from tests.fakes import IN_WORK, card, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

UTC = timezone.utc

PC_REPORT = """Проверка перед уроком v1.4 — 30.09.2026 14:05
Система: Windows 10 Pro 22H2
[OK] Камера: Integrated Webcam
[!] Звук: Звук выключен
Исправлено: Камера разрешена в конфиденциальности
#lesson-check 1.4 problems=cam.privacy,snd.muted left=snd.muted"""
WEB_REPORT = """Проверка перед уроком — 30.09.2026 14:10
[X] Камера: нет разрешения
[!] Интернет: 3.1 Мбит/с — видео может подтормаживать
#lesson-check-web 1 problems=web.cam.perm,web.net.netSlow"""


def at_local(e, y, m, d, hh=12):
    return datetime(y, m, d, hh, tzinfo=e.cfg.tz).astimezone(UTC)


def take(e, card_id, when):
    e.storage.claim_card(card_id, f"Заявка {card_id}", f"https://x/{card_id}", when, when + timedelta(hours=4),
                         when + timedelta(hours=3))


def team(e, card_id, when):
    e.storage.team_seen(card_id, title="t", lane="", topic="", created_at=when, entered_at=when, now=when,
                        responsible_id=None, responsible="", in_queue=False, urgent=False)


# ---------------- разбор строки ----------------

def test_parse_pc_web_deep_crash():
    pc, = parse(PC_REPORT)
    assert (pc.source, pc.version, pc.problems, pc.left, pc.fixed) == \
        ("pc", "1.4", ["cam.privacy", "snd.muted"], ["snd.muted"], ["cam.privacy"])
    web, = parse(WEB_REPORT)
    assert (web.source, web.problems, web.left) == ("web", ["web.cam.perm", "web.net.netSlow"], None)
    deep, = parse("...\n#lesson-check 1.4 deep=1 steps=3")
    assert (deep.source, deep.steps) == ("deep", 3)
    crash, = parse("#lesson-check 1.4 problems=mic.muted left=mic.muted crashed=1")
    assert crash.crashed and crash.problems == ["mic.muted"]
    ok, = parse("#lesson-check 1.4 problems=- left=-")
    assert ok.problems == [] and ok.left == []
    assert parse("просто текст без строки") == [] and parse("#lesson-check 1.4") == []


def test_parse_survives_kaiten_markup():
    # редактор Kaiten: markdown-экранирование и HTML
    md = PC_REPORT.replace("#lesson-check", "\\#lesson\\-check").replace("cam.privacy", "cam\\.privacy") \
        .replace("snd.muted", "snd\\_x").replace("snd\\_x", "snd.muted")
    assert parse(md)[0].problems == ["cam.privacy", "snd.muted"]
    htm = "<p>Проверка</p><p>#lesson-check 1.4 problems=cam.policy_apps,net.vpn&nbsp;left=net.vpn</p>"
    pc, = parse(htm)
    assert pc.problems == ["cam.policy_apps", "net.vpn"] and pc.left == ["net.vpn"]
    tagged, = parse("<p>Итог</p><p><span>#lesson-check 1.4 problems=cam.none,<em>mic.none</em> left=-</span></p>")
    assert tagged.problems == ["cam.none", "mic.none"] and tagged.left == []
    esc = "#lesson-check 1.4 problems=cam.policy\\_apps left=-"
    assert parse(esc)[0].problems == ["cam.policy_apps"]


def test_parse_rejects_garbage_and_keeps_left_inside_problems():
    pc, = parse("#lesson-check 1.4 problems=cam.none,<script>,DROP;TABLE,x left=mic.none")
    assert pc.problems == ["cam.none", "mic.none"] and pc.left == ["mic.none"]
    two = parse(PC_REPORT + "\n\nещё раз:\n#lesson-check 1.4 problems=- left=-")
    assert [c.problems for c in two] == [["cam.privacy", "snd.muted"], []]


def test_every_code_has_a_russian_label():
    assert label("cam.privacy") == "камера запрещена в конфиденциальности"
    assert label("web.cam.perm") == "страница: камера — нет разрешения"
    assert all(v and v != k for k, v in PC_LABELS.items())


# ---------------- откуда берём ----------------

@run
async def test_scan_reads_comments_of_my_and_team_cards_once():
    async with env() as e:
        now = e.clock.now
        take(e, 1, now - timedelta(hours=2))
        team(e, 2, now - timedelta(days=1))
        team(e, 3, now - timedelta(days=20))     # слишком старая — не читаем
        e.kaiten.card_comments = {
            1: [{"id": 11, "text": PC_REPORT.replace("#lesson-check", "\\#lesson\\-check"),
                 "created": "2026-09-23T05:00:00Z"}],
            2: [{"id": 21, "text": "<p>" + WEB_REPORT.replace("\n", "<br>") + "</p>"},
                {"id": 22, "text": "Взял в работу"}],
            3: [{"id": 31, "text": PC_REPORT}],
        }
        assert await e.relay.lessons.scan() == 2
        assert sorted(e.kaiten.comment_list_calls) == [1, 2]
        rows = e.relay.lessons.rows()
        assert {(r["card_id"], r["source"], r["origin"]) for r in rows} == {(1, "pc", "kaiten"), (2, "web", "kaiten")}
        assert next(r for r in rows if r["card_id"] == 1)["at"] == datetime(2026, 9, 23, 5, tzinfo=UTC)
        # сразу второй проход ничего не читает; через 6 часов перечитывает, но не дублирует
        e.kaiten.comment_list_calls.clear()
        assert await e.relay.lessons.scan() == 0 and e.kaiten.comment_list_calls == []
        e.clock.now += timedelta(hours=6, minutes=1)
        e.kaiten.card_comments[1].append({"id": 12, "text": "#lesson-check 1.4 problems=- left=-"})
        assert await e.relay.lessons.scan() == 1
        assert sorted(e.kaiten.comment_list_calls) == [1, 2] and len(e.relay.lessons.rows()) == 3


@run
async def test_scan_batches_and_survives_a_broken_card():
    async with env() as e:
        now = e.clock.now
        for cid in range(1, SCAN_BATCH + 11):
            team(e, cid, now - timedelta(hours=1))
            e.kaiten.card_comments[cid] = [{"id": cid, "text": f"#lesson-check 1.4 problems=pc.ram left=pc.ram"}]
        e.kaiten.fail_comment_list = {1}
        assert await e.relay.lessons.scan() == SCAN_BATCH - 1      # 40 карточек, одна сломана
        assert len(e.kaiten.comment_list_calls) == SCAN_BATCH
        e.kaiten.comment_list_calls.clear()
        assert await e.relay.lessons.scan() == 10                   # потом — оставшиеся 10 (сломанная — позже)
        assert 1 not in e.kaiten.comment_list_calls


@run
async def test_report_sent_to_chat_is_counted_and_not_doubled_by_kaiten():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        take(e, 1, e.clock.now)
        keys = ["username:client_one"]
        await e.relay.on_chat_activity(keys, incoming=True, manual=False, at=e.clock.now, text=PC_REPORT)
        rows = e.relay.lessons.rows()
        assert [(r["card_id"], r["origin"]) for r in rows] == [(1, "chat")]
        # тот же отчёт ты вставил в карточку — это та же проверка
        e.kaiten.card_comments[1] = [{"id": 5, "text": PC_REPORT}]
        assert await e.relay.lessons.scan() == 0
        # сообщение без строки ничего не добавляет, обычная работа не ломается
        await e.relay.on_chat_activity(keys, incoming=True, manual=False, at=e.clock.now, text="спасибо")
        assert len(e.relay.lessons.rows()) == 1


@run
async def test_chat_report_without_single_card_still_counted():
    async with env() as e:
        await e.relay.on_chat_activity(["username:nobody"], incoming=True, manual=False, at=e.clock.now,
                                       text=WEB_REPORT)
        rows = e.relay.lessons.rows()
        assert [(r["card_id"], r["source"]) for r in rows] == [(None, "web")]


@run
async def test_only_codes_are_stored():
    async with env() as e:
        await e.relay.on_chat_activity(["username:client_one"], incoming=True, manual=False, at=e.clock.now,
                                       text=PC_REPORT + "\nмой телефон +996 555 123456")
        dump = "\n".join(str(tuple(r)) for r in e.storage._exec("SELECT * FROM lesson_checks"))
        assert "cam.privacy" in dump
        for secret in ("555", "Windows", "Integrated", "client_one", "телефон"):
            assert secret not in dump


# ---------------- что видит Билл ----------------

async def _week_of_checks(e):
    s = e.relay.lessons
    day = at_local(e, 2026, 9, 30)
    s.record(parse(PC_REPORT), day, "kaiten", 1, "a")
    s.record(parse("#lesson-check 1.4 problems=cam.privacy left=-"), day, "kaiten", 2, "b")
    s.record(parse("#lesson-check 1.4 problems=- left=-"), day, "kaiten", 3, "c")
    s.record(parse(WEB_REPORT), day, "chat", None, "d")
    s.record(parse("#lesson-check 1.4 deep=1 steps=2"), day, "kaiten", 2, "e")
    s.record(parse("#lesson-check 1.4 problems=mic.muted left=mic.muted crashed=1"), day, "kaiten", 4, "f")
    s.record(parse(PC_REPORT), at_local(e, 2026, 10, 6), "kaiten", 9, "other-week")


@run
async def test_checks_command_shows_top_fixed_and_left():
    async with env() as e:
        await _week_of_checks(e)
        e.clock.now = at_local(e, 2026, 10, 7)
        control = Control(e.relay, None, "1000")
        await control.handle(owner_msg("/checks last"))
        text = e.alert.sent[-1]
        assert text.startswith("🩺 Проверки перед уроком за неделю 28.09–04.10: 5 (программа 4, страница 1)")
        assert "Всё в порядке сразу: 1 (20%)" in text
        assert "• камера запрещена в конфиденциальности — 2 (40%)" in text
        assert "Программа исправила на месте:\n• камера запрещена в конфиденциальности — 2" in text
        assert "Не исправилось (нужен человек):\n• микрофон выключен — 1\n• звук выключен — 1" in text
        assert "глубокий ремонт: 1" in text and "программа упала: 1" in text
        assert "страница: камера — нет разрешения — 1" in text
        await control.handle(owner_msg("/checks"))                 # эта неделя: только одна проверка
        assert ": 1 (программа 1, страница 0)" in e.alert.sent[-1]
        await control.handle(owner_msg("/checks month", chat_id=4242))  # чужим — ничего
        assert len(e.alert.sent) == 2


@run
async def test_checks_empty_period_says_so():
    async with env() as e:
        await Control(e.relay, None, "1000").handle(owner_msg("/checks"))
        assert "пока ни одной" in e.alert.sent[-1]


@run
async def test_checks_csv_is_a_file_without_personal_data():
    async with env() as e:
        await _week_of_checks(e)
        control = Control(e.relay, None, "1000")
        await control.handle(owner_msg("/checks csv"))
        name, data = e.alert.files[-1]
        assert name == "lesson-checks.csv" and data.startswith(b"\xef\xbb\xbf")
        lines = data.decode("utf-8-sig").splitlines()
        assert lines[0] == "дата;проверка;откуда;версия;карточка;код;проблема;исправлено"
        assert "2026-09-30;1;программа;1.4;1;cam.privacy;камера запрещена в конфиденциальности;да" in lines
        assert "2026-09-30;1;программа;1.4;1;snd.muted;звук выключен;нет" in lines
        assert any(";-;всё в порядке;" in line for line in lines)
        assert any(";web.cam.perm;страница: камера — нет разрешения;" in line for line in lines)
        assert any(";глубокий ремонт;" in line and "шагов: 2" in line for line in lines)
        assert "@" not in data.decode("utf-8-sig") and "Integrated" not in data.decode("utf-8-sig")


@run
async def test_csv_file_kept_next_to_database(tmp_path=None):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        async with env(DB_PATH=str(Path(tmp) / "data" / "state.db")) as e:
            e.relay.lessons.record(parse(PC_REPORT), e.clock.now, "kaiten", 1, "x")
            path = Path(tmp) / "data" / "lesson-checks.csv"
            assert path.exists() and "cam.privacy" in path.read_text(encoding="utf-8-sig")


@run
async def test_weekly_report_has_checks_section_only_when_there_are_checks():
    async with env() as e:
        r = Reporter(e.relay)
        assert "Проверки ПК" not in r.text(r.period("week", date(2026, 10, 5)))
        await _week_of_checks(e)
        text = r.text(r.period("week", date(2026, 10, 5)))
        assert "🩺 Проверки ПК перед уроком:\n• проверок: 5 (программа 4, страница 1)" in text
        assert "• чаще всего: камера запрещена в конфиденциальности — 2" in text

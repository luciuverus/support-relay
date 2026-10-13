"""Отчёт за неделю и месяц: действия бота из журнала, сэкономленное время, команда."""
from datetime import date, datetime, timedelta, timezone

from app.control import Control
from app.incidents import IncidentDetector
from app.reports import Reporter
from tests.fakes import card, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

UTC = timezone.utc


def at_local(e, y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=e.cfg.tz).astimezone(UTC)


def fill_journal(e, day):
    when = at_local(e, *day)
    for i, action in enumerate(["взял в работу", "написал автору (@a)", "написал клиенту (@c)",
                                "написал «актуально ли?» (@a)", "перенёс в «Ждём ответа»",
                                "закрыл после «оцените работу»", "закрыл: клиент не ответил (категория «X»)",
                                "не смог написать автору: нет чата", "[тест] взял бы в работу", "взял в работу"]):
        e.storage.journal_add(when + timedelta(minutes=i), 100 + i, action)


def test_report_periods():
    class R(Reporter):
        def __init__(self, cfg):
            self.cfg = cfg
    from tests.fakes import make_cfg
    r = R(make_cfg())
    w = r.period("week", date(2026, 10, 5))                     # понедельник
    assert (w.since.date(), w.until.date(), w.title) == (date(2026, 9, 28), date(2026, 10, 5), "неделю 28.09–04.10")
    assert r.period("week", date(2026, 10, 7), previous=False).since.date() == date(2026, 10, 5)
    m = r.period("month", date(2026, 1, 1))
    assert (m.since.date(), m.until.date(), m.title) == (date(2025, 12, 1), date(2026, 1, 1), "декабрь 2025")


@run
async def test_report_counts_actions_hours_and_skips_test_mode():
    async with env() as e:
        fill_journal(e, (2026, 9, 30))
        e.storage.journal_add(at_local(e, 2026, 10, 6), 1, "взял в работу")     # другая неделя
        r = Reporter(e.relay)
        text = r.text(r.period("week", date(2026, 10, 5)))
        assert text.startswith("📈 Отчёт за неделю 28.09–04.10")
        assert "• взял заявок в работу: 2" in text                              # «[тест]» и чужая неделя не считаются
        assert "• поздоровался с автором: 1" in text and "• поздоровался с клиентом: 1" in text
        assert "• спросил «актуально ли?»: 1" in text and "• закрыл после оценки: 1" in text
        assert "• закрыл «клиент не ответил»: 1" in text
        # 1+1+1.5+1.5+1+0.25+0.75+1 = 8 мин
        assert "≈ сэкономлено 0.1 ч" in text
        assert "• не смог написать (написал ты): 1" in text


@run
async def test_report_includes_team_numbers():
    async with env() as e:
        d = IncidentDetector(e.cfg, e.storage, e.relay.kaiten, e.alert, e.clock, e.relay.is_paused, "Улугбек")
        e.clock.now = at_local(e, 2026, 9, 30)
        e.kaiten.board["lanes"] = [{"id": 1, "title": "1 уровень"}]
        e.kaiten.cards[1] = {"id": 1, "title": "Вопрос", "column_id": 11, "board_id": 900, "lane_id": 1,
                             "created": e.clock.now.isoformat(), "members": [{"id": 888, "type": 2, "full_name": "Тимур"}]}
        await d.poll()
        e.clock.now += timedelta(minutes=3)
        e.kaiten.cards[1]["column_id"] = 55
        await d.poll()
        r = Reporter(e.relay, d)
        text = r.text(r.period("week", date(2026, 10, 5)))
        assert "• новых заявок: 1 — 1 уровень 1" in text and "взято за 5 минут: 100% (1 из 1)" in text
        assert "– Тимур: 1, медиана 3 мин" in text and "массовых сбоев и завалов: 0" in text


@run
async def test_report_sent_monday_once_to_bill_and_manager():
    async with env(WORK_HOURS="11:30-20:30") as e:
        d = IncidentDetector(e.cfg, e.storage, e.relay.kaiten, e.alert, e.clock, e.relay.is_paused, "Улугбек")
        d.set_manager("555")
        r = Reporter(e.relay, d)
        fill_journal(e, (2026, 9, 30))
        e.clock.now = at_local(e, 2026, 10, 5, 10, 0)            # понедельник, до начала работы
        assert await r.tick() == []
        e.clock.now = at_local(e, 2026, 10, 5, 11, 40)
        assert await r.tick() == ["week:2026-W40"]
        assert e.alert.sent[-1].startswith("📈 Отчёт за неделю 28.09–04.10")
        assert d.manager_sent[-1] == e.alert.sent[-1]
        assert await r.tick() == []                              # второй раз — нет
        e.clock.now = at_local(e, 2026, 10, 6, 12, 0)            # вторник
        assert await r.tick() == []


@run
async def test_report_monthly_on_first_and_switches():
    async with env(WORK_HOURS="11:30-20:30") as e:
        r = Reporter(e.relay)
        e.clock.now = at_local(e, 2026, 10, 1, 12, 0)            # четверг, 1-е
        assert await r.tick() == ["month:2026-09"]
        assert e.alert.sent[-1].startswith("📈 Отчёт за сентябрь 2026")
        assert await r.tick() == []
    async with env(WORK_HOURS="11:30-20:30", REPORT_WEEKLY="false", REPORT_MONTHLY="false") as e:
        r = Reporter(e.relay)
        e.clock.now = at_local(e, 2026, 6, 1, 12, 0)             # понедельник и 1-е
        assert await r.tick() == []


@run
async def test_report_command_only_for_owner():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.clock.now = at_local(e, 2026, 10, 7)
        e.storage.journal_add(at_local(e, 2026, 10, 6), 1, "взял в работу")
        await control.handle(owner_msg("/report"))
        assert e.alert.sent[-1].startswith("📈 Отчёт за неделю 05.10–11.10") and "взял заявок в работу: 1" in e.alert.sent[-1]
        await control.handle(owner_msg("/report month"))
        assert e.alert.sent[-1].startswith("📈 Отчёт за октябрь 2026")
        await control.handle(owner_msg("/report last"))
        assert e.alert.sent[-1].startswith("📈 Отчёт за неделю 28.09–04.10")
        before = len(e.alert.sent)
        await control.handle(owner_msg("/report", chat_id=555))
        assert len(e.alert.sent) == before


@run
async def test_report_real_flow_is_counted():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        r = Reporter(e.relay)
        today = e.clock.now.astimezone(e.cfg.tz).date()
        text = r.text(r.period("week", today, previous=False))
        assert "взял заявок в работу: 1" in text and "поздоровался с автором: 1" in text

"""Цифры команды в отчётах бота подписаны «по данным бота», точные SLA — в дашборде (ссылка из .env)."""
from datetime import date, timedelta

from app.incidents import IncidentDetector
from app.reports import Reporter
from tests.fakes import run
from tests.test_relay import env
from tests.test_report import at_local

URL = "https://script.google.com/macros/s/AKfy-test/exec"


async def _team_week(e):
    d = IncidentDetector(e.cfg, e.storage, e.relay.kaiten, e.alert, e.clock, e.relay.is_paused, "Улугбек")
    e.clock.now = at_local(e, 2026, 9, 30)
    e.kaiten.board["lanes"] = [{"id": 1, "title": "1 уровень"}]
    e.kaiten.cards[1] = {"id": 1, "title": "Вопрос", "column_id": 11, "board_id": 900, "lane_id": 1,
                         "created": e.clock.now.isoformat(), "members": [{"id": 888, "type": 2, "full_name": "Тимур"}]}
    await d.poll()
    e.clock.now += timedelta(minutes=3)
    e.kaiten.cards[1]["column_id"] = 55
    await d.poll()
    return d


@run
async def test_sourcenote_weekly_report_with_dashboard_link():
    async with env(DASHBOARD_URL=URL) as e:
        d = await _team_week(e)
        text = Reporter(e.relay, d).text(Reporter(e.relay, d).period("week", date(2026, 10, 5)))
        assert "👥 Команда (по данным бота):" in text
        assert f"Точные SLA и KPI — в дашборде: {URL}" in text
        team = text.split("👥 Команда (по данным бота):", 1)[1]
        assert team.index("взято за 5 минут") < team.index("по данным бота (опрос")   # подпись — под цифрами


@run
async def test_sourcenote_without_link_and_without_team_numbers():
    async with env() as e:
        d = await _team_week(e)
        r = Reporter(e.relay, d)
        text = r.text(r.period("week", date(2026, 10, 5)))
        assert "Точные SLA и KPI — в дашборде SLA/KPI" in text and "https://" not in text
        empty = r.text(r.period("week", date(2026, 9, 21)))                 # неделя без заявок команды
        assert "по данным бота" not in empty


@run
async def test_sourcenote_daily_digest():
    async with env(DASHBOARD_URL=URL) as e:
        d = await _team_week(e)
        text = d.digest_text(d._day_start(e.clock.now), e.clock.now)
        assert "Взято за 5 минут" in text and text.splitlines()[-1].endswith(f"в дашборде: {URL}")

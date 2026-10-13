"""Массовый сбой — только когда заявки про ОДНУ проблему, а не просто одной темы."""
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

from app.storage import Storage
from tests.fakes import run
from tests.test_incidents import detector, tcard, with_manager
from tests.test_relay import env

QUIET = dict(UNTAKEN_ALERT_MIN="0", BACKLOG_LIMIT="0")


def answer_field(e):
    e.kaiten.props.append({"id": 50, "name": "Какая у вас проблема со счетом?", "type": "select"})
    e.kaiten.select_values[50] = [{"id": 901, "value": "Клиент не может оплатить"},
                                  {"id": 902, "value": "Другое (напишу в комментариях)"},
                                  {"id": 903, "value": "Удалить счёт"}]


@run
async def test_masssimilar_different_bitrix_problems_are_not_an_incident():
    async with env(**QUIET) as e:
        d = with_manager(e, detector(e))
        tcard(e, 1, "Не создаётся сделка в битриксе", lane=3)
        tcard(e, 2, "Удалить лишний счёт за оплату", lane=3)
        tcard(e, 3, "Пустая ссылка на оплату", lane=3)
        tcard(e, 4, "Перенести оплату на другую дату", lane=3)
        assert await d.poll() == []
        assert d.manager_sent == [] and not any("сбой" in t for t in e.alert.sent)


@run
async def test_masssimilar_one_shared_word_is_not_enough():
    async with env(**QUIET) as e:
        d = detector(e)
        tcard(e, 1, "Не открывается ссылка на оплату в браузере", lane=3)
        tcard(e, 2, "Ссылка на оплату не создаётся для сделки", lane=3)
        tcard(e, 3, "Нужна ссылка на оплату рассрочки", lane=3)
        assert await d.poll() == []


@run
async def test_masssimilar_same_problem_in_different_words_is_an_incident():
    async with env(**QUIET) as e:
        d = with_manager(e, detector(e))
        tcard(e, 1, "Оплата картой не РФ не проходит", lane=3)
        tcard(e, 2, "Клиент не может оплатить картой не рф", lane=3)
        tcard(e, 3, "оплата иностранной картой — нет варианта не рф", lane=3)
        tcard(e, 4, "Удалить лишний счёт", lane=3)                      # другая проблема — не в счёт
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]
        mine = next(t for t in e.alert.sent if "массовый сбой" in t)
        assert "3 заявки похожих" in mine and "Удалить лишний" not in mine


@run
async def test_masssimilar_same_form_answer_groups_even_with_names_as_titles():
    async with env(**QUIET) as e:
        answer_field(e)
        d = with_manager(e, detector(e))
        for i, title in ((1, "Оплата Иванова"), (2, "счет Чикаго"), (3, "оплата, медресе")):
            tcard(e, i, title, lane=3, properties={"id_50": [901]})
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]
        assert "Общее: «Клиент не может оплатить»" in d.manager_sent[0]


@run
async def test_masssimilar_answer_other_does_not_group():
    async with env(**QUIET) as e:
        answer_field(e)
        d = detector(e)
        for i, title in ((1, "Оплата Иванова"), (2, "оплата Чикаго"), (3, "оплата Сидорова")):
            tcard(e, i, title, lane=3, properties={"id_50": [902]})
        assert await d.poll() == []


@run
async def test_masssimilar_words_common_this_week_do_not_count():
    async with env(**QUIET) as e:
        d = detector(e)
        e.clock.now -= timedelta(days=2)
        for i in range(100, 140):                                    # «медресе» — в каждой четвёртой заявке недели
            tcard(e, i, "медресе урок" if i % 4 == 0 else f"разное {i} вопрос{i}", lane=1)
        await d.poll()
        e.clock.now += timedelta(days=2)
        for cid in list(e.kaiten.cards):
            e.kaiten.cards.pop(cid)
        tcard(e, 1, "битрикс медресе Иванов", lane=3)
        tcard(e, 2, "оплата медресе", lane=3)
        tcard(e, 3, "медресе битрикс сделка", lane=3)
        assert await d.poll() == []


@run
async def test_masssimilar_answers_unreadable_still_works_by_text():
    async with env(**QUIET) as e:
        e.kaiten.fail_props = True
        d = detector(e)
        for i in (1, 2, 3):
            tcard(e, i, f"Не приходит чек об оплате клиенту {i}", lane=3)
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]


@run
async def test_masssimilar_switch_off_restores_topic_only_rule():
    async with env(INCIDENT_BY_TEXT="false", **QUIET) as e:
        d = detector(e)
        tcard(e, 1, "Не создаётся сделка в битриксе", lane=3)
        tcard(e, 2, "Удалить лишний счёт за оплату", lane=3)
        tcard(e, 3, "Пустая ссылка на оплату", lane=3)
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]


def test_masssimilar_old_database_gets_new_columns():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "state.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE team_cards (card_id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                     "lane TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
                     "entered_at TEXT NOT NULL, first_seen TEXT NOT NULL, responsible_id INTEGER, "
                     "responsible TEXT NOT NULL DEFAULT '', in_queue INTEGER NOT NULL DEFAULT 1, left_queue_at TEXT, "
                     "untaken_alerted INTEGER NOT NULL DEFAULT 0, escalated INTEGER NOT NULL DEFAULT 0, "
                     "urgent INTEGER NOT NULL DEFAULT 0)")
        conn.commit(); conn.close()
        s = Storage(path)
        cols = {r["name"] for r in s._exec("PRAGMA table_info(team_cards)")}
        assert {"words", "answers"} <= cols
        s.close()

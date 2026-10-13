"""Похожие решённые заявки: импорт выгрузки, подсказка при взятии, /similar."""
import json
import os
import tempfile
from pathlib import Path

from openpyxl import Workbook

from app.control import Control
from app.similar import Case, CaseBase, CaseStore, cases_from_xlsx, import_export
from app.tools import import_cases
from tests.fakes import card, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

HEADER = ["Название", "ID", "Дата создания", "Дорожка", "Опишите вашу проблему подробно:",
          "Какая у вас проблема со счетом?", "Фактическая категория проблемы (Селект)",
          "Фактическая категория проблемы (Строка)", "Заказчик"]


def make_xlsx(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)


ROWS = [
    ["Оплата иностранной картой", "#101", "2026-08-10 10:00:00", "Внутренний",
     "Клиент не может оплатить картой не РФ, почта mama@example.com, тел +7 900 000-00-00, https://x.bitrix24.ru/deal/5",
     "Клиент не может оплатить Счет", "Смена способа оплаты", "смена метода оплаты на карту не РФ",
     "Иванова Ольга (m@x.ru)"],
    ["Сёмин Илья 1с не приходит ссылка на урок", "#102", "2026-08-11 10:00:00", "1-ый уровень", "ученик @vasya не видит ссылку",
     "Другое", "LMS", "пересоздал ссылку на урок", ""],
    ["Оплата", "#103", "2026-08-12", "Внутренний", "клиент пропал", "", "Другое", "Нет ответа", ""],
    ["Вопрос", "#104", "2026-08-12", "1-ый уровень", "что-то", "", "Решилось само/неактуально", "само прошло", ""],
    ["Без итога", "#105", "2026-08-12", "1-ый уровень", "оплата", "", "", "", ""],
]


def write_cases(path, cases):
    CaseBase(cases).save(path)


PAY = Case(101, "Оплата иностранной картой", "клиент не может оплатить картой не рф счет",
           "смена метода оплаты на карту не РФ", "Смена способа оплаты", "Внутренний", "2026-08-10")
LINK = Case(102, "Не приходит ссылка на урок", "ученик не видит ссылку", "пересоздал ссылку на урок", "LMS",
            "1-ый уровень", "2026-08-11")


# ---------------- импорт ----------------

def test_similar_bot_import_scrubs_personal_data_and_skips_useless():
    with tempfile.TemporaryDirectory() as tmp:
        xlsx = Path(tmp) / "summary-report.xlsx"
        make_xlsx(xlsx, ROWS)
        cases = cases_from_xlsx(xlsx)
        assert [c.card_id for c in cases] == [101, 102]
        dump = json.dumps([c.__dict__ for c in cases], ensure_ascii=False)
        for secret in ("Сёмин", "Илья", "mama", "mail.ru", "931", "bitrix24", "@vasya", "Иванова", "m@x.ru"):
            assert secret not in dump, secret
        assert cases[0].solution == "смена метода оплаты на карту не РФ" and cases[0].created == "2026-08-10"
        assert "Клиент не может оплатить Счет" in cases[0].problem
        out = Path(tmp) / "data" / "cases.json"
        lines = import_export(xlsx, out)
        assert out.exists() and "2 решённых заявок" in lines[0]


def test_similar_bot_import_command_takes_newest_xlsx_in_folder():
    with tempfile.TemporaryDirectory() as tmp:
        make_xlsx(Path(tmp) / "old.xlsx", ROWS[:1])
        newest = Path(tmp) / "new.xlsx"
        make_xlsx(newest, ROWS)
        os.utime(newest, (2_000_000_000, 2_000_000_000))
        old_env = os.environ.pop("CASES_FILE", None)
        try:
            assert import_cases([], folder=Path(tmp)) == 0
        finally:
            if old_env is not None:
                os.environ["CASES_FILE"] = old_env
        saved = json.loads((Path(tmp) / "data" / "cases.json").read_text(encoding="utf-8"))
        assert len(saved) == 2
        assert import_cases([str(Path(tmp) / "missing.xlsx")], folder=Path(tmp)) == 1
        (Path(tmp) / "bad.xlsx").write_text("not excel")
        assert import_cases([str(Path(tmp) / "bad.xlsx")], folder=Path(tmp)) == 1


def test_similar_bot_wrong_file_explains():
    with tempfile.TemporaryDirectory() as tmp:
        xlsx = Path(tmp) / "x.xlsx"
        wb = Workbook()
        wb.active.append(["Колонка", "Другая"])
        wb.save(xlsx)
        try:
            cases_from_xlsx(xlsx)
        except ValueError as exc:
            assert "сводный отчёт" in str(exc)
        else:
            raise AssertionError("no error")


# ---------------- поиск ----------------

def test_similar_bot_search_finds_by_meaning_words_not_exact_text():
    base = CaseBase([PAY, LINK])
    found = base.search("Родитель не может оплатить счёт иностранной картой")
    assert found and found[0].card_id == 101
    assert base.search("погода в бишкеке") == []
    assert base.search("не могу оплатить") == []   # одно общее слово — это ещё не «похожий случай»
    assert base.search("оплата иностранной картой", exclude_id=101) == []


def test_similar_bot_store_reloads_new_file_without_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        store = CaseStore(path)
        assert store.get() is None and store.search("оплата") == []
        write_cases(path, [LINK])
        assert store.search("оплата иностранной картой") == []
        write_cases(path, [PAY, LINK])
        os.utime(path, (2_000_000_000, 2_000_000_000))
        assert [c.card_id for c in store.search("оплата иностранной картой")] == [101]
        path.write_text("{битый")
        os.utime(path, (2_000_000_100, 2_000_000_100))
        assert store.get() is None


# ---------------- бот ----------------

@run
async def test_similar_bot_take_alert_shows_past_solutions():
    with tempfile.TemporaryDirectory() as tmp:
        cases = Path(tmp) / "cases.json"
        write_cases(cases, [PAY, LINK])
        async with env(CASES_FILE=str(cases)) as e:
            e.kaiten.cards[7] = card(7, title="Не проходит оплата иностранной картой")
            assert await e.relay.poll_queue() == ["taken"]
            text = e.alert.sent[-1]
            assert "💡 Похожие случаи" in text and "смена метода оплаты на карту не РФ" in text
            assert "https://x.kaiten.ru/card/101" in text and "пересоздал" not in text


@run
async def test_similar_bot_no_hint_without_base_or_match_or_when_off():
    with tempfile.TemporaryDirectory() as tmp:
        cases = Path(tmp) / "cases.json"
        async with env(CASES_FILE=str(cases)) as e:
            e.kaiten.cards[7] = card(7, title="Не проходит оплата иностранной картой")
            await e.relay.poll_queue()
            assert "Похожие" not in e.alert.sent[-1]
        write_cases(cases, [PAY])
        async with env(CASES_FILE=str(cases)) as e:
            e.kaiten.cards[8] = card(8, title="Сломалась доска Miro")
            await e.relay.poll_queue()
            assert "Похожие" not in e.alert.sent[-1]
        async with env(CASES_FILE=str(cases), SIMILAR_HINTS="0") as e:
            e.kaiten.cards[9] = card(9, title="Не проходит оплата иностранной картой")
            await e.relay.poll_queue()
            assert "Похожие" not in e.alert.sent[-1]
        write_cases(cases, [Case(9, "Не проходит оплата иностранной картой", "", "та же самая карточка", "", "", "")])
        async with env(CASES_FILE=str(cases)) as e:
            e.kaiten.cards[9] = card(9, title="Не проходит оплата иностранной картой")
            await e.relay.poll_queue()
            assert "та же самая" not in e.alert.sent[-1]


@run
async def test_similar_bot_command_only_for_owner():
    with tempfile.TemporaryDirectory() as tmp:
        cases = Path(tmp) / "cases.json"
        async with env(CASES_FILE=str(cases)) as e:
            control = Control(e.relay, None, "1000")
            await control.handle(owner_msg("/similar оплата"))
            assert "База похожих случаев пустая" in e.alert.sent[-1]
            write_cases(cases, [PAY, LINK])
            await control.handle(owner_msg("/similar ссылка на урок не приходит"))
            assert "пересоздал ссылку" in e.alert.sent[-1]
            await control.handle(owner_msg("/similar"))
            assert "Напиши после команды" in e.alert.sent[-1]
            await control.handle(owner_msg("/similar абвгд"))
            assert "не нашёл" in e.alert.sent[-1]
            before = len(e.alert.sent)
            await control.handle(owner_msg("/similar оплата", chat_id=555))
            assert len(e.alert.sent) == before

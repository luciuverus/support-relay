import random
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from app.humanize import MessageBook, TemplateError, greeting, next_send_time, typing_seconds

TZ = ZoneInfo("Asia/Bishkek")
WORK = dict(tz=TZ, start=time(9), end=time(18), days=frozenset(range(1, 6)))


def _send(now_local: datetime) -> datetime:
    return next_send_time(now_local.astimezone(timezone.utc), rng=random.Random(1),
                          delay_min_s=40, delay_max_s=150, **WORK).astimezone(TZ)


def test_in_work_hours_small_human_delay():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=TZ)  # среда
    sent = _send(now)
    assert 40 <= (sent - now).total_seconds() <= 150


def test_after_hours_moves_to_next_morning():
    sent = _send(datetime(2026, 9, 23, 19, 30, tzinfo=TZ))  # среда вечер
    assert sent.date().isoformat() == "2026-09-24" and time(9) <= sent.time() <= time(9, 15)


def test_before_hours_same_day():
    sent = _send(datetime(2026, 9, 23, 7, 0, tzinfo=TZ))
    assert sent.date().isoformat() == "2026-09-23" and time(9) <= sent.time() <= time(9, 15)


def test_friday_evening_and_weekend_go_to_monday():
    for start in (datetime(2026, 9, 25, 18, 5, tzinfo=TZ), datetime(2026, 9, 26, 12, 0, tzinfo=TZ)):
        sent = _send(start)
        assert sent.isoweekday() == 1 and sent.date().isoformat() == "2026-09-28"


def test_typing_seconds_bounds():
    rng = random.Random(0)
    assert typing_seconds("ок", rng, 4, 7) == 2.0
    assert typing_seconds("x" * 10_000, rng, 4, 7) == 45.0
    mid = typing_seconds("x" * 100, rng, 4, 7)
    assert 100 / 7 <= mid <= 100 / 4


def test_greeting():
    assert greeting(datetime(2026, 1, 1, 8, tzinfo=TZ)) == "Доброе утро"
    assert greeting(datetime(2026, 1, 1, 13, tzinfo=TZ)) == "Добрый день"
    assert greeting(datetime(2026, 1, 1, 20, tzinfo=TZ)) == "Добрый вечер"


def test_message_book_no_immediate_repeat_and_all_fields_filled():
    book = MessageBook.load("tests/messages_single.json", random.Random(3))
    values = {"title": "T", "card_id": 1, "deadline": "сегодня до 15:00",
              "deadline_time": "15:00", "greeting": "Добрый день"}
    last = None
    for _ in range(50):
        bubbles, idx = book.render("client_taken", values, last)
        assert idx != last
        assert all("{" not in b and "}" not in b for b in bubbles)
        last = idx


def test_message_book_rejects_unknown_fields_and_missing_kinds():
    with pytest.raises(TemplateError):
        MessageBook({"kaiten_comment": [], "employee_taken": ["{oops}"], "client_taken": ["ok"]})
    with pytest.raises(TemplateError):
        MessageBook({"employee_taken": ["ok"], "client_taken": ["ok"]})


@pytest.mark.parametrize("bad, reason", [
    ({"kaiten_comment": ["{}"], "employee_taken": [], "client_taken": []}, "пустые скобки"),
    ({"kaiten_comment": ["до {deadline"], "employee_taken": [], "client_taken": []}, "сломанный шаблон"),
    ({"kaiten_comment": "строка", "employee_taken": [], "client_taken": []}, "список"),
    ({"kaiten_comment": [""], "employee_taken": [], "client_taken": []}, "непустой"),
    ({"employee_taken": [], "client_taken": []}, "kaiten_comment"),
    (["не объект"], "объектом"),
])
def test_texts_validation_messages(bad, reason):
    with pytest.raises(TemplateError, match=reason):
        MessageBook(bad)


def test_texts_disabled_kind_renders_none():
    book = MessageBook({"kaiten_comment": [], "employee_taken": ["a"], "client_taken": ["b"]})
    assert not book.enabled("kaiten_comment") and book.render("kaiten_comment", {}) is None

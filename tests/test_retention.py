"""Срок хранения: старые тексты сообщений, чаты и заявки удаляются раз в сутки."""
from datetime import timedelta

from app.storage import Storage
from tests.fakes import Clock, card, run
from tests.test_relay import env


@run
async def test_old_messages_chats_and_cards_are_pruned_daily():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 2
        e.storage.remember_chat(["username:client_one"], 5151, e.clock.now, 1)
        e.storage.set("author_key:1", "username:author_person")
        assert e.relay.prune_journal()
        assert len(e.storage.all_messages()) == 2 and e.storage.find_chat("username:client_one")
        # через 61 день тексты удалены, через 91 — чат, заявка и отметки по ней
        e.clock.now += timedelta(days=61)
        assert e.relay.prune_journal()
        assert e.storage.all_messages() == [] and e.storage.has_card(1)
        e.clock.now += timedelta(days=30)
        assert e.relay.prune_journal()
        assert not e.storage.has_card(1) and e.storage.find_chat("username:client_one") is None
        assert e.storage.get("author_key:1") is None and not e.storage.with_prefix("greeted:1:")
        assert not e.relay.prune_journal()                      # не чаще раза в сутки


def test_pending_and_held_messages_and_recent_chats_are_kept():
    s, clock = Storage(":memory:"), Clock()
    old = clock.now - timedelta(days=100)
    s.enqueue(1, "client", "@a", "ждёт отправки", old, old, "x")
    s.enqueue(2, "client", "@b", "ждёт, пока напишет", old, old, "y")
    s.fail_group(2, "client", "unknown chat", "held")
    s.remember_chat(["username:fresh"], 7, clock.now - timedelta(days=1), 1)
    s.remember_chat(["username:silent"], 8, None, None)          # без отметок времени — не трогаем
    s.prune_old(clock.now - timedelta(days=60), clock.now - timedelta(days=90))
    assert len(s.all_messages()) == 2
    assert s.find_chat("username:fresh") and s.find_chat("username:silent")


def test_contact_marks_and_who_questions_are_pruned():
    s, clock = Storage(":memory:"), Clock()
    now = clock.now
    s.remember_chat(["username:alive"], 7, now, 1)
    s.set("last_variant:followup:username:alive", "1")
    s.set("last_variant:followup:username:gone", "0")
    s.set("last_variant:kaiten_comment", "0")                         # без контакта — не трогаем
    s.set("whoq:old", '{"keys": ["username:a"], "text": "секрет", "at": "%s"}' % (now - timedelta(days=2)).isoformat())
    s.set("whoq:new", '{"keys": ["username:a"], "text": "текст", "at": "%s"}' % now.isoformat())
    s.set("whoq:legacy", '{"keys": ["username:a"], "text": "без даты"}')
    s.set("who:username:a", f"me@{(now - timedelta(days=2)).isoformat()}")
    s.set("who:username:b", f"me@{now.isoformat()}")
    s.claim_card(5, "t", "u", now, now, now)
    s.set("author_key:5", "username:a")                                # своя открытая заявка — нужен
    s.set("author_key:6", "username:b")                                # чужая — это кеш, удаляем
    s.set("fill:6", "{}")
    s.prune_old(now - timedelta(days=60), now - timedelta(days=90), now - timedelta(days=1))
    kept = set(s.with_prefix(""))
    assert {"last_variant:followup:username:alive", "last_variant:kaiten_comment", "whoq:new",
            "who:username:b", "author_key:5"} <= kept
    assert not kept & {"last_variant:followup:username:gone", "whoq:old", "whoq:legacy", "who:username:a",
                       "author_key:6", "fill:6"}


def test_tracebacks_are_redacted_in_the_log():
    import logging
    from app.main import SecretFilter
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None)
    try:
        raise RuntimeError("token kaiten-secret-123 and 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    SecretFilter(["kaiten-secret-123"]).filter(record)
    text = logging.Formatter("%(message)s").format(record)
    assert "kaiten-secret-123" not in text and "ABCDEFGHIJ" not in text and "RuntimeError" in text

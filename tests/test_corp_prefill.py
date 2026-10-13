"""Готовый текст — в чат корпоративного аккаунта с ботом: кнопка оттуда открывает чат с человеком
сразу в корпоративном аккаунте, а не в личном аккаунте специалиста."""
from datetime import timedelta

from app.control import Control
from app.service import CORP_HINT
from app.transports import SendError
from tests.fakes import business_connection_update, card, run
from tests.test_relay import RecordingTransport, env


class CorpTransport(RecordingTransport):
    def __init__(self, works=True):
        super().__init__()
        self.works = works
        self.owner_messages: list[tuple[str, list]] = []
        self.deleted: list[int] = []

    async def notify_owner(self, text, buttons=None):
        if not self.works:
            return None
        self.owner_messages.append((text, buttons))
        return 100 + len(self.owner_messages)

    async def delete_owner_message(self, message_id):
        self.deleted.append(message_id)


async def held_greeting(e, transport):
    e.relay.transport = transport
    e.kaiten.cards[1] = card(1, client=None)
    await e.relay.poll_queue()
    transport.fail["username:author_person"] = SendError("не писал", reason="unknown_chat")
    e.clock.now += timedelta(minutes=5)
    await e.relay.send_due()


@run
async def test_held_greeting_goes_to_corporate_chat_and_is_removed_when_person_writes():
    async with env() as e:
        t = CorpTransport()
        await held_greeting(e, t)
        text, buttons = t.owner_messages[0]
        assert "№1" in text and "@author_person" in text and "Улугбек" in text
        url = buttons[0][0]["url"]
        assert url.startswith("https://t.me/author_person?text=")
        alert = next(a for a in e.alert.sent if a.startswith("⏳"))
        assert CORP_HINT in alert and "переключись" not in alert
        # человек написал сам — бот отправит приветствие, готовый текст в чате аккаунта уже не нужен
        del t.fail["username:author_person"]
        await e.relay.on_chat_activity(["id:4242", "username:author_person"], incoming=True, manual=False,
                                       at=e.clock.now, text="привет")
        assert t.deleted == [101]


@run
async def test_manual_write_removes_ready_text_also_after_a_hard_failure():
    async with env() as e:
        t = CorpTransport()
        e.relay.transport = t
        e.kaiten.cards[1] = card(1, client=None)
        await e.relay.poll_queue()
        t.fail["username:author_person"] = SendError("не писал больше 24 часов", reason="window")
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert t.owner_messages and "готовый текст я отправил" in e.alert.sent[-1].lower()
        await e.relay.on_chat_activity(["id:4242", "username:author_person"], incoming=False, manual=True,
                                       at=e.clock.now, text="Добрый день, по заявке 1")
        assert t.deleted == [101]
        assert not e.storage.with_prefix("corpmsg:")


@run
async def test_manual_button_removes_ready_text():
    async with env() as e:
        t = CorpTransport()
        await held_greeting(e, t)
        await Control(e.relay, None, "1000").handle(
            {"update_id": 9, "callback_query": {"id": "q", "from": {"id": 1000}, "data": "manual:1:employee"}})
        assert t.deleted == [101]


@run
async def test_without_corporate_chat_the_alert_says_to_switch_account_and_asks_once_for_start():
    async with env() as e:
        t = CorpTransport(works=False)
        await held_greeting(e, t)
        alert = next(a for a in e.alert.sent if a.startswith("⏳"))
        assert "переключись на корпоративный аккаунт" in alert
        assert sum("нажми /start" in a for a in e.alert.sent) == 1
        e.kaiten.cards[2] = card(2, client=None, title="ещё одна")
        await e.relay.poll_queue()
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert sum("нажми /start" in a for a in e.alert.sent) == 1


@run
async def test_no_ready_text_for_phone_contacts_or_when_switched_off():
    async with env(CORP_PREFILL="false") as e:
        t = CorpTransport()
        await held_greeting(e, t)
        assert t.owner_messages == []
    async with env() as e:
        t = CorpTransport()
        e.relay.transport = t
        assert await e.relay._corp_card(1, "client", "+79001234567", "текст") is False
        assert t.owner_messages == []


@run
async def test_business_transport_writes_to_the_account_chat_and_deletes():
    from tests.test_transports import BizEnv

    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        assert e.t.owner_chat() == 42
        mid = await e.t.notify_owner("готовый текст", [[{"text": "x", "url": "https://t.me/a?text=b"}]])
        sent = e.bot.params("sendMessage")[-1]
        assert sent["chat_id"] == 42 and "business_connection_id" not in sent and mid
        await e.t.delete_owner_message(mid)
        assert e.bot.params("deleteMessage")[-1] == {"chat_id": 42, "message_id": mid}


@run
async def test_corporate_account_opening_the_bot_is_not_a_stranger():
    async with env() as e:
        e.storage.set("business_owner_chat", "42")
        from app.incidents import IncidentDetector
        detector = IncidentDetector(e.cfg, e.storage, e.relay.kaiten, e.alert, is_paused=e.relay.is_paused,
                                    name=e.relay.name)
        control = Control(e.relay, None, "1000", detector=detector)
        await control.handle({"update_id": 1, "message": {"chat": {"id": 42, "type": "private"},
                                                          "from": {"id": 42}, "text": "/start"}})
        assert e.alert.sent == []


@run
async def test_account_start_gets_a_confirmation_from_the_connected_bot():
    from tests.test_transports import BizEnv

    async with BizEnv() as e:
        await e.t.handle_update(business_connection_update())
        handled = []

        async def extra(update):
            handled.append(update)
        e.t.extra_handler = extra
        e.storage.set("corp_chat_warned", "1")
        await e.t.handle_update({"update_id": 5, "message": {"chat": {"id": 42, "type": "private"},
                                                             "from": {"id": 42}, "text": "/start"}})
        reply = e.bot.params("sendMessage")[-1]
        assert reply["chat_id"] == 42 and "этот чат подключён" in reply["text"] and handled == []
        assert e.storage.get("corp_chat_warned") == ""
        await e.t.handle_update({"update_id": 6, "message": {"chat": {"id": 1000, "type": "private"},
                                                             "from": {"id": 1000}, "text": "/status"}})
        assert len(handled) == 1   # команды специалистов — как раньше

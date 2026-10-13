import asyncio
import random
from datetime import timedelta

import aiohttp

import json
import os
import shutil
import tempfile

from app.humanize import MessageStore
from app.kaiten import KaitenClient
from app.logic import Contact, normalize_people
from app.service import Relay
from app.storage import Storage
from app.transports import AlertBot, SendError, Transport
from tests.fakes import IN_WORK, MY_ID, Clock, FakeKaiten, card, make_cfg, run


class RecordingTransport(Transport):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[Contact, str]] = []
        self.fail: dict[str, SendError] = {}

    async def send(self, contact, text, still_wanted=None):
        if contact.key in self.fail:
            raise self.fail[contact.key]
        self.sent.append((contact, text))


class Env:
    async def __aenter__(self):
        self.kaiten = FakeKaiten()
        base = await self.kaiten.start()
        self.session = aiohttp.ClientSession()
        self.clock = Clock()
        self.cfg = make_cfg(KAITEN_BASE_URL=base, **getattr(self, "overrides", {}))
        self.storage = Storage(":memory:")
        self.transport = RecordingTransport()
        self.tmp = tempfile.mkdtemp()
        self.texts_path = os.path.join(self.tmp, "messages.json")
        shutil.copy("tests/messages_single.json", self.texts_path)
        self.texts = MessageStore(self.texts_path, random.Random(1))
        self.alert = AlertBot(None, "1000")
        self.relay = Relay(self.cfg, self.storage, KaitenClient(base, "t", self.session, backoff_s=0),
                           self.transport, self.alert, self.texts,
                           normalize_people({"500": "@author_person"}), random.Random(2), self.clock)
        return self

    def write_texts(self, data):
        """Переписать messages.json, как будто ты сохранил его в редакторе."""
        with open(self.texts_path, "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
        st = os.stat(self.texts_path)
        os.utime(self.texts_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    def read_texts(self):
        with open(self.texts_path, encoding="utf-8") as fh:
            return json.load(fh)

    async def __aexit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)
        await self.session.close()
        await self.kaiten.close()


def env(**overrides):
    e = Env()
    e.overrides = overrides
    return e


@run
async def test_assigned_card_is_taken_and_messages_queued():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert e.kaiten.patches == [(1, {"column_id": IN_WORK})]
        assert len(e.kaiten.comments) == 1 and "Взял в работу" in e.kaiten.comments[0][1]
        assert e.alert.sent and "Не работает оплата 1" in e.alert.sent[0]
        queued = e.storage.all_messages()
        assert {m["role"] for m in queued} == {"employee", "client"}
        # ничего не уходит мгновенно — человеческая пауза 40..150 с
        assert await e.relay.send_due() == 0
        e.clock.now += timedelta(seconds=39)
        assert await e.relay.send_due() == 0
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 2
        assert {c.key for c, _ in e.transport.sent} == {"username:author_person", "username:client_one"}
        assert all("{" not in text for _, text in e.transport.sent)


@run
async def test_duplicate_webhooks_do_not_duplicate_work():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        results = await asyncio.gather(*[e.relay.handle_webhook({"data": {"card_id": 1}}) for _ in range(5)])
        assert results.count("taken") == 1
        assert len(e.kaiten.patches) == 1 and len(e.kaiten.comments) == 1
        assert len(e.storage.all_messages()) == 2
        assert await e.relay.handle_webhook({"data": {"card_id": 1}}) == "ignored:already-in-work"


@run
async def test_reopened_card_is_taken_again():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        e.kaiten.cards[1]["column_id"] = 11  # вернули в «Очередь»
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert len(e.kaiten.patches) == 2 and len(e.storage.open_cards()) == 1


@run
async def test_dry_run_does_not_repeat_itself():
    async with env(DRY_RUN="true") as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "ignored:already-taken"
        assert len(e.storage.all_messages()) == 2


@run
async def test_not_mine_or_already_in_work_is_ignored():
    async with env() as e:
        e.kaiten.cards[2] = card(2, responsible=123)
        e.kaiten.cards[3] = card(3, column=IN_WORK)
        e.kaiten.cards[4] = card(4, members=[{"id": MY_ID, "type": 1}])
        assert await e.relay.handle_webhook({"data": {"id": 2}}) == "ignored:not-mine"
        assert await e.relay.handle_webhook({"data": {"id": 3}}) == "ignored:already-in-work"
        assert await e.relay.handle_webhook({"data": {"id": 4}}) == "ignored:not-mine"
        assert await e.relay.handle_webhook({"data": {"id": 404}}) == "ignored:card-not-found"
        assert await e.relay.handle_webhook({"event": "ping"}) == "ignored:no-card"
        assert e.kaiten.patches == [] and e.storage.all_messages() == []


@run
async def test_queue_only_waiting_cards_are_never_touched():
    async with env() as e:
        e.kaiten.cards[5] = card(5, column=77)  # «Ждём ответа», ответственный — ты
        e.kaiten.cards[6] = card(6, column=78)  # «Проблемные»
        for _ in range(3):  # клиент пишет, карточку комментируют — вебхуки идут
            assert await e.relay.handle_webhook({"data": {"card_id": 5}}) == "ignored:not-in-queue"
            assert await e.relay.handle_webhook({"data": {"card_id": 6}}) == "ignored:not-in-queue"
        assert e.kaiten.patches == [] and e.kaiten.comments == []
        assert e.storage.all_messages() == [] and e.alert.sent == []


@run
async def test_queue_only_taken_card_moved_to_waiting_is_left_alone():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        e.kaiten.cards[1]["column_id"] = 77  # ты перевёл в «Ждём ответа»
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "ignored:not-in-queue"
        assert len(e.kaiten.patches) == 1 and len(e.storage.all_messages()) == 2


@run
async def test_queue_only_several_queue_columns_supported():
    async with env(QUEUE_COLUMN_IDS="11, 12") as e:
        e.kaiten.cards[7] = card(7, column=12)
        assert await e.relay.handle_webhook({"data": {"id": 7}}) == "taken"


@run
async def test_kaiten_failure_releases_card_for_retry():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        e.kaiten.fail_patch = True
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "error:kaiten"
        assert "Не смог взять" in e.alert.sent[-1]
        assert e.storage.all_messages() == []
        e.kaiten.fail_patch = False
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"


@run
async def test_unknown_author_and_bad_client_contact_are_reported():
    async with env() as e:
        e.kaiten.cards[1] = card(1, owner=31337, client="позвоните мне")
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert "employees.json" in e.alert.sent[0] and "контакт клиента «позвоните мне» — не Telegram" in e.alert.sent[0]
        assert e.storage.all_messages() == []


@run
async def test_send_failure_alerts_and_flood_wait_reschedules():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        e.transport.fail["username:client_one"] = SendError("окно 24 часа закрыто")
        e.transport.fail["username:author_person"] = SendError("flood", retry_after_s=60)
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 0
        statuses = {m["role"]: m["status"] for m in e.storage.all_messages()}
        assert statuses == {"client": "failed", "employee": "pending"}
        assert any("окно 24 часа" in a for a in e.alert.sent)
        e.transport.fail.clear()
        e.clock.now += timedelta(minutes=2)
        assert await e.relay.send_due() == 1


@run
async def test_dry_run_touches_nothing_external():
    async with env(DRY_RUN="true") as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert e.kaiten.patches == [] and e.kaiten.comments == []
        e.clock.now += timedelta(minutes=5)
        assert await e.relay.send_due() == 2
        assert e.transport.sent == []
        assert e.alert.sent[0].startswith("[тест")


@run
async def test_regulation_greeting_after_hours_not_postponed():
    """Регламент 4.2: взял в работу — связался сразу, даже если это 20:29 или суббота."""
    async with env(WORK_HOURS="11:30-20:30", WORK_DAYS="1-5") as e:
        e.clock.now = e.clock.now.replace(hour=14, minute=29)  # 20:29 по Бишкеку
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        assert "нерабочее время" not in e.alert.sent[0]
        e.clock.now += timedelta(minutes=3)                     # 20:32 — уже «нерабочее»
        assert await e.relay.send_due() == 2


@run
async def test_sla_warn_then_breach_while_card_stays_in_work():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        e.alert.sent.clear()
        e.clock.now += timedelta(minutes=100)
        assert await e.relay.check_sla() == 0
        e.clock.now += timedelta(minutes=100)  # 200 из 240 минут = 83%
        assert await e.relay.check_sla() == 1 and "До SLA" in e.alert.sent[-1]
        assert await e.relay.check_sla() == 0  # не спамит
        e.clock.now += timedelta(minutes=41)
        assert await e.relay.check_sla() == 1 and "просрочен" in e.alert.sent[-1]
        assert await e.relay.check_sla() == 0


@run
async def test_sla_silent_when_card_moved_out_of_work():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        e.alert.sent.clear()
        e.kaiten.cards[1]["column_id"] = 99  # решено / ждём клиента
        e.clock.now += timedelta(hours=10)
        assert await e.relay.check_sla() == 0 and e.alert.sent == []
        assert e.storage.open_cards() == []


# ---------- тексты из config/messages.json ----------

@run
async def test_texts_kaiten_comment_and_messages_come_from_file():
    async with env() as e:
        data = e.read_texts()
        data["kaiten_comment"] = ["L2 забрал, ответ до {deadline_full} (№{card_id})"]
        data["client_taken"] = ["{greeting}, смотрим ваш вопрос, ответим {deadline}"]
        data["employee_taken"] = ["Взял «{title}»"]
        e.write_texts(data)
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert e.kaiten.comments[0][1].startswith("L2 забрал, ответ до ") and "(№1)" in e.kaiten.comments[0][1]
        texts = {m["role"]: m["text"] for m in e.storage.all_messages()}
        assert texts["employee"] == "Взял «Не работает оплата 1»"
        assert texts["client"].startswith("Добрый день, смотрим ваш вопрос, ответим сегодня до ")


@run
async def test_texts_empty_list_switches_action_off():
    async with env() as e:
        data = e.read_texts()
        data["kaiten_comment"] = []
        data["client_taken"] = []
        data["employee_taken"] = []
        e.write_texts(data)
        e.kaiten.cards[1] = card(1, owner=31337)  # автор не в employees.json — но и писать ему не нужно
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert e.kaiten.patches and e.kaiten.comments == []
        assert e.storage.all_messages() == []
        assert "employees.json" not in e.alert.sent[0]


@run
async def test_texts_edit_is_picked_up_without_restart():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        e.kaiten.cards[2] = card(2)
        await e.relay.handle_webhook({"data": {"id": 1}})
        data = e.read_texts()
        data["client_taken"] = ["Новый текст для №{card_id}"]
        e.write_texts(data)
        await e.relay.handle_webhook({"data": {"id": 2}})
        client_texts = [m["text"] for m in e.storage.all_messages() if m["role"] == "client"]
        assert client_texts[0] != "Новый текст для №1" and client_texts[1] == "Новый текст для №2"


@run
async def test_texts_broken_file_keeps_old_texts_and_alerts_once():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        e.kaiten.cards[2] = card(2)
        good = e.read_texts()
        e.write_texts('{"kaiten_comment": ["Взял"], "client_taken": [')  # недописанный JSON
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        errors = [a for a in e.alert.sent if "messages.json" in a]
        assert len(errors) == 1 and "строке" in errors[0]
        assert e.kaiten.comments[0][1].startswith("Взял в работу. Срок по SLA:")  # старый текст
        await e.relay.send_due()
        assert len([a for a in e.alert.sent if "messages.json" in a]) == 1  # не спамит
        good["kaiten_comment"] = ["Исправленный комментарий"]
        e.write_texts(good)
        assert await e.relay.handle_webhook({"data": {"id": 2}}) == "taken"
        assert e.kaiten.comments[-1][1] == "Исправленный комментарий"


@run
async def test_texts_bad_template_is_reported_with_reason():
    async with env() as e:
        data = e.read_texts()
        data["client_taken"] = ["Ответим до {deadlin}"]
        e.write_texts(data)
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        error = next(a for a in e.alert.sent if "messages.json" in a)
        assert "deadlin" in error and "Можно:" in error
        client = [m["text"] for m in e.storage.all_messages() if m["role"] == "client"][0]
        assert "{" not in client


@run
async def test_texts_dry_run_previews_what_would_be_written():
    async with env(DRY_RUN="true") as e:
        data = e.read_texts()
        data["kaiten_comment"] = ["Комментарий-превью {card_id}"]
        data["client_taken"] = ["Клиенту-превью {card_id}"]
        e.write_texts(data)
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        assert "Kaiten: Комментарий-превью 1" in e.alert.sent[0]
        assert "client: Клиенту-превью 1" in e.alert.sent[0]
        assert e.kaiten.comments == []


# ---------- опрос Kaiten без вебхука ----------

@run
async def test_polling_takes_my_queue_card_once():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.poll_queue() == ["taken"]
        assert e.kaiten.patches == [(1, {"column_id": IN_WORK})]
        assert await e.relay.poll_queue() == []  # карточка уже в «В работе», в очереди её нет
        assert len(e.kaiten.patches) == 1 and len(e.storage.all_messages()) == 2
        assert e.kaiten.list_calls[0]["column_id"] == "11"


@run
async def test_polling_ignores_other_people_and_other_columns():
    async with env() as e:
        e.kaiten.cards[2] = card(2, responsible=123)          # чужая в очереди
        e.kaiten.cards[3] = card(3, column=77)                # моя, но в «Ждём ответа»
        e.kaiten.cards[4] = card(4, members=[{"id": MY_ID, "type": 1}])  # я лишь участник
        assert await e.relay.poll_queue() == []
        assert e.kaiten.patches == [] and e.kaiten.get_calls == []  # лишних запросов нет


@run
async def test_polling_falls_back_to_full_card_when_list_has_no_members():
    async with env() as e:
        e.kaiten.list_without_members = True
        e.kaiten.cards[1] = card(1)
        e.kaiten.cards[2] = card(2, responsible=123)
        assert sorted(await e.relay.poll_queue()) == ["ignored:not-mine", "taken"]


@run
async def test_polling_reads_all_pages():
    async with env() as e:
        for i in range(1, 106):
            e.kaiten.cards[i] = card(i, responsible=123)
        e.kaiten.cards[106] = card(106)
        assert await e.relay.poll_queue() == ["taken"]
        assert [c["offset"] for c in e.kaiten.list_calls] == ["0", "100"]


@run
async def test_polling_failure_alerts_once_then_recovers():
    async with env() as e:
        e.kaiten.cards[1] = card(1)
        e.kaiten.fail_patch = True
        for _ in range(3):
            assert await e.relay.poll_queue() == ["error:kaiten"]
        assert len([a for a in e.alert.sent if "Не смог взять" in a]) == 1
        e.kaiten.fail_patch = False
        assert await e.relay.poll_queue() == ["taken"]

"""Кому писать по уровню: 2-й — клиенту (родителю/ученику) + короткое куратору; 1-й и внутренний — автору."""
from datetime import timedelta

from app.logic import client_contact_from_text
from tests.fakes import card, run
from tests.test_relay import env

LANES = [{"id": 1, "title": "1-ый уровень"}, {"id": 2, "title": "2-ой уровень"}, {"id": 3, "title": "Внутренний сервис"}]
LEVEL_ENV = {"CLIENT_LANES": "2-ой уровень,2-й уровень,второй уровень", "CLIENT_TG_FIELD": ""}
TEXTS = {"kaiten_comment": ["Взял"], "employee_taken": ["Привет автору {card_id}"], "client_taken": ["Привет клиенту {card_id}"],
         "curator_note": ["Взял {card_id}, пишу клиенту"]}


def test_levels_client_contact_found_in_free_text():
    assert client_contact_from_text("@Sara_Sardorova тг мамы ученика") == "@Sara_Sardorova"
    assert client_contact_from_text("https://t.me/mama_ali") == "@mama_ali"
    assert client_contact_from_text("Madina_K") == "@Madina_K"
    assert client_contact_from_text("89387005077 УЧИТЕЛЬ") == "+79387005077"
    assert client_contact_from_text("+996 555 123 456") == "+996555123456"
    for nothing in ("только ва у них, я буду им передавать. инфу", "89129199595 ватсап.пока нет тг у родителей((",
                    ".", "неважно", "Тпп", "Ольга Смирнова", ""):
        assert client_contact_from_text(nothing) == "", nothing


async def _take(e, lane, client):
    e.write_texts(TEXTS)
    e.kaiten.board["lanes"] = LANES
    e.kaiten.cards[1] = card(1, lane_id=lane, client=client)
    assert await e.relay.poll_queue() == ["taken"]
    e.clock.now += timedelta(minutes=5)
    await e.relay.send_due()
    return {c.key: t for c, t in e.transport.sent}


@run
async def test_levels_second_level_greets_client_and_notes_curator():
    async with env(**LEVEL_ENV) as e:
        sent = await _take(e, 2, "@Sara_Sardorova тг мамы ученика")
        assert sent == {"username:sara_sardorova": "Привет клиенту 1", "username:author_person": "Взял 1, пишу клиенту"}
        assert [c.key for c, _ in e.transport.sent][0] == "username:sara_sardorova"   # сначала клиенту


@run
async def test_levels_second_level_without_telegram_greets_curator_and_warns():
    async with env(**LEVEL_ENV) as e:
        sent = await _take(e, 2, "только ва у них, я буду им передавать. инфу")
        assert sent == {"username:author_person": "Привет автору 1"}
        assert "не Telegram, пишу только автору" in e.alert.sent[0]


@run
async def test_levels_second_level_phone_client_is_told_to_bill():
    async with env(**LEVEL_ENV) as e:
        e.transport.fail["phone:+79387005077"] = __import__("app.transports", fromlist=["SendError"]).SendError("по номеру нельзя")
        sent = await _take(e, 2, "89387005077 УЧИТЕЛЬ")
        assert sent == {"username:author_person": "Взял 1, пишу клиенту"}
        assert any("+79387005077" in a and "Напиши вручную" in a for a in e.alert.sent)


@run
async def test_levels_first_level_and_internal_only_author():
    for lane in (1, 3):
        async with env(**LEVEL_ENV) as e:
            sent = await _take(e, lane, "@student_ali")
            assert sent == {"username:author_person": "Привет автору 1"}, lane


@run
async def test_levels_unknown_lane_only_author():
    async with env(**LEVEL_ENV) as e:
        sent = await _take(e, None, "@student_ali")
        assert sent == {"username:author_person": "Привет автору 1"}


@run
async def test_levels_empty_setting_means_all_levels_as_before():
    async with env(CLIENT_LANES="") as e:
        sent = await _take(e, 1, "@student_ali")
        assert sent == {"username:author_person": "Привет автору 1", "username:student_ali": "Привет клиенту 1"}


PARENT = ["id:5555", "username:sara_sardorova"]
CAT, SOL, DONE, WAITING = "id_45", "id_46", 90, 77


async def _l2_card(e, **kw):
    e.write_texts(TEXTS)
    e.kaiten.board["lanes"] = LANES
    e.kaiten.cards[1] = card(1, lane_id=2, client="@Sara_Sardorova тг мамы", **kw)
    e.kaiten.cards[900] = card(900, column=55, responsible=123, owner=999001)   # чтобы бот нашёл доску
    await e.relay.waiting_column_ids()


@run
async def test_levels_writing_to_parent_moves_card_to_work():
    async with env(**LEVEL_ENV) as e:
        await _l2_card(e, column=WAITING)
        await e.relay.on_chat_activity(PARENT, incoming=False, manual=True, at=e.clock.now, text="Добрый день, проверил")
        assert e.kaiten.cards[1]["column_id"] == 55


@run
async def test_levels_rating_sent_to_parent_closes_card():
    async with env(**LEVEL_ENV) as e:
        await _l2_card(e, column=55, properties={"id_42": "@Sara_Sardorova тг мамы", CAT: [501], SOL: "сделал"})
        await e.relay.on_chat_activity(PARENT, incoming=False, manual=True, at=e.clock.now,
                                       text="Пожалуйста оцените качество работы специалиста по заявке по 5-балльной шкале 😊")
        assert e.kaiten.cards[1]["column_id"] == DONE


@run
async def test_levels_wait_hint_watches_parent_chat_on_second_level():
    async with env(**LEVEL_ENV, WAIT_HINT_MIN="30") as e:
        await _l2_card(e, column=55)
        e.storage.remember_chat(PARENT, 5555, e.clock.now - timedelta(hours=2), 1,
                                last_out_at=e.clock.now - timedelta(minutes=40))
        assert await e.relay.poll_wait_hints() == ["hint:1"]

"""Telegram автора из поля «Ваш телеграм» и номер заявки у каждой карточки свой."""
import json
from datetime import timedelta

from app.logic import normalize_people
from app.tools import describe_card
from tests.fakes import card, run
from tests.test_bubbles_author import sd_card
from tests.test_relay import env


def ticket(card_id, tg="@curator_tg", column=11):
    data = sd_card(card_id, column=column)
    data["properties"] = {"id_43": tg} if tg is not None else {}
    return data


def shipped(e):
    with open("config/messages.default.json", encoding="utf-8") as fh:
        e.write_texts(json.load(fh))


@run
async def test_tg_field_author_from_card_field_found_by_name():
    async with env() as e:  # в .env только название поля, id не нужен
        assert e.cfg.author_tg_field == "Ваш телеграм"
        e.relay.employees = {}
        e.kaiten.cards[70951934] = ticket(70951934)
        e.kaiten.cards[70951935] = ticket(70951935, tg="t.me/other_person")
        assert await e.relay.poll_queue() == ["taken", "taken"]
        roles = {(m["card_id"], m["role"]): m["contact"] for m in e.storage.all_messages()}
        assert roles[(70951934, "employee")] == "@curator_tg"
        assert roles[(70951935, "employee")] == "t.me/other_person"
        assert e.kaiten.props_calls == 1  # список полей кешируется
        e.clock.now += timedelta(minutes=5)
        await e.relay.send_due()
        assert "username:curator_tg" in {c.key for c, _ in e.transport.sent}
        assert not any("Не нашёл в Kaiten поле" in a for a in e.alert.sent)


@run
async def test_tg_field_empty_falls_back_to_employees_json():
    async with env() as e:
        e.relay.employees = normalize_people({"curator@example.com": "@from_file"})
        e.kaiten.cards[1] = ticket(1, tg=None)
        await e.relay.poll_queue()
        assert [m["contact"] for m in e.storage.all_messages() if m["role"] == "employee"] == ["@from_file"]


@run
async def test_tg_field_empty_and_no_fallback_warns_you():
    async with env() as e:
        e.relay.employees = {}
        e.kaiten.cards[1] = ticket(1, tg=None)
        await e.relay.poll_queue()
        assert "пустое поле «Ваш телеграм»" in e.alert.sent[0]


@run
async def test_tg_field_wrong_name_alerts_once_with_similar_names():
    async with env(AUTHOR_TG_FIELD="Мой телеграм") as e:
        e.relay.employees = {}
        e.kaiten.cards[1] = ticket(1)
        e.kaiten.cards[2] = ticket(2)
        await e.relay.poll_queue()
        alerts = [a for a in e.alert.sent if "Не нашёл в Kaiten поле" in a]
        assert len(alerts) == 1 and "Ваш телеграм" in alerts[0]


@run
async def test_tg_field_by_id_needs_no_lookup():
    for spec in ("id_43", "43"):
        async with env(AUTHOR_TG_FIELD=spec) as e:
            e.relay.employees = {}
            e.kaiten.cards[1] = ticket(1)
            await e.relay.poll_queue()
            assert e.kaiten.props_calls == 0
            assert [m["contact"] for m in e.storage.all_messages() if m["role"] == "employee"] == ["@curator_tg"]


@run
async def test_tg_field_api_error_does_not_break_taking():
    async with env() as e:
        e.kaiten.props = "boom"  # сломанный ответ
        e.relay.employees = normalize_people({"500": "@author_person"})
        e.kaiten.cards[1] = card(1)
        assert await e.relay.poll_queue() == ["taken"]


@run
async def test_tg_field_helper_shows_field_names_and_source():
    async with env() as e:
        e.kaiten.cards[70951934] = ticket(70951934)
        text = "\n".join(await describe_card(e.cfg, e.relay.kaiten, {}, 70951934))
        assert "Ваш телеграм [id_43]: @curator_tg" in text
        assert "Автору напишем: @curator_tg (взял из: поле «Ваш телеграм»)" in text
        assert "№70951934" in text


@run
async def test_card_number_per_card_in_messages():
    async with env() as e:
        shipped(e)
        e.relay.employees = {}
        for cid in (70951934, 70960001, 71000042):
            e.kaiten.cards[cid] = ticket(cid, tg=f"@person_{cid}")
        await e.relay.poll_queue()
        numbers = {}
        for row in e.storage.all_messages():
            if row["role"] == "employee" and row["text"].isdigit():
                numbers[row["card_id"]] = row["text"]
        assert numbers == {70951934: "70951934", 70960001: "70960001", 71000042: "71000042"}
        with open("config/messages.default.json", encoding="utf-8") as fh:
            raw = fh.read()
        assert "70951934" not in raw and "{card_id}" in raw  # в шаблоне не зашит конкретный номер

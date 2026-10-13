"""Быстрые ответы: «ждём ответ» → «Ждём ответа», «закрываем»/«оценка» → фактические поля Kaiten, /learn."""
import tempfile
from pathlib import Path

from app.control import Control
from app.similar import Case, CaseBase
from tests.fakes import IN_WORK, card, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

WAITING, RECONTACT, DONE = 77, 79, 90
KEYS = ["id:4242", "username:author_person"]
CAT, SOL = "id_45", "id_46"


async def say(e, text, keys=KEYS):
    await e.relay.on_chat_activity(keys, incoming=False, manual=True, at=e.clock.now, text=text)


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


def all_buttons(e):
    return [b.get("callback_data") for rows in e.alert.sent_buttons if rows for row in rows for b in row]


# ---------------- «ждём ответ» ----------------

@run
async def test_quick_wait_moves_to_waiting_and_does_not_bounce_back():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await say(e, "Ожидаем вашего ответа 🙏")
        assert e.kaiten.cards[1]["column_id"] == WAITING
        assert "Ждём ответа" in e.alert.sent[-1] and "work:1" in all_buttons(e)
        # кнопка «Вернуть в работу»
        control = Control(e.relay, None, "1000")
        await control.handle(cb("work:1"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK


@run
async def test_quick_wait_ordinary_message_still_moves_to_work():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=WAITING)
        e.kaiten.cards[5] = card(5, column=IN_WORK, responsible=999, owner=600)   # чтобы бот нашёл доску
        await say(e, "Добрый день, посмотрел вашу проблему")
        assert e.kaiten.cards[1]["column_id"] == IN_WORK


@run
async def test_quick_wait_several_cards_ask_and_foreign_untouched():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        e.kaiten.cards[2] = card(2, column=IN_WORK)
        e.kaiten.cards[3] = card(3, column=IN_WORK, responsible=999, owner=600)
        await say(e, "Ждём вашего ответа")
        assert {c["column_id"] for c in e.kaiten.cards.values()} == {IN_WORK}
        assert "wait:1" in all_buttons(e) and "wait:2" in all_buttons(e) and "wait:3" not in all_buttons(e)


@run
async def test_quick_wait_other_chat_not_moved():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await say(e, "Ожидаем вашего ответа", keys=["id:1", "username:someone_else"])
        assert e.kaiten.cards[1]["column_id"] == IN_WORK


# ---------------- «закрываем» / «оценка работы» ----------------

@run
async def test_quick_close_asks_category_with_guesses_and_writes_both_fields():
    with tempfile.TemporaryDirectory() as tmp:
        cases = Path(tmp) / "cases.json"
        CaseBase([Case(101, "Оплата иностранной картой", "клиент не может оплатить картой не рф",
                       "смена метода оплаты на карту не РФ", "Смена способа оплаты", "", "2026-08-10"),
                  Case(102, "Оплата картой не РФ не проходит", "оплата иностранной картой",
                       "сменил тип оплаты", "Смена способа оплаты", "", "2026-08-11")]).save(cases)
        async with env(CASES_FILE=str(cases)) as e:
            e.kaiten.cards[1] = card(1, column=IN_WORK, title="Не проходит оплата иностранной картой")
            control = Control(e.relay, None, "1000")
            await say(e, "Закрываем заявку?")
            ask = e.alert.sent[-1]
            assert "Категория проблемы?" in ask and "№1" in ask
            buttons = e.alert.sent_buttons[-1]
            assert buttons[0][0] == {"text": "Смена способа оплаты", "callback_data": "fcat:1:503"}
            assert buttons[-1][0]["callback_data"] == "fall:1"
            await say(e, "Закрываем заявку?")          # второй раз не спрашиваем
            assert e.alert.sent.count(ask) == 1
            await control.handle(cb("fcat:1:503"))
            assert e.kaiten.cards[1]["properties"][CAT] == [503]
            assert "Что сделали по №1?" in e.alert.sent[-1]
            assert e.alert.sent_buttons[-1][0][0]["callback_data"] == "fsol:1:0"
            await control.handle(owner_msg("Сменили метод оплаты на «картой не РФ»"))
            assert e.kaiten.cards[1]["properties"][SOL] == "Сменили метод оплаты на «картой не РФ»"
            assert e.kaiten.cards[1]["properties"]["id_42"] == "@Client_One"   # остальные поля не тронуты
            assert e.alert.sent[-1].startswith("✅") and "когда отправишь /оценкаработы" in e.alert.sent[-1]
            assert e.kaiten.cards[1]["column_id"] == IN_WORK            # «закрываем?» ещё не закрывает
            # дальше обычный текст в личку — не пишется в заявку
            before = len(e.kaiten.patches)
            await control.handle(owner_msg("просто сообщение"))
            assert len(e.kaiten.patches) == before


@run
async def test_quick_close_solution_button_and_scalar_shape_from_filled_cards():
    async with env() as e:
        e.kaiten.cards[99] = card(99, column=DONE, properties={CAT: 502, SOL: "было"})
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        control = Control(e.relay, None, "1000")
        await say(e, "Можно закрывать заявку?")
        await control.handle(cb("fcat:1:501"))
        assert e.kaiten.cards[1]["properties"][CAT] == 501          # как на заполненных карточках
        e.relay.quick._save_state(1, {**e.relay.quick._state(1), "solutions": ["пересоздал ссылку"]})
        await control.handle(cb("fsol:1:0"))
        assert e.kaiten.cards[1]["properties"][SOL] == "пересоздал ссылку"
        await control.handle(cb("fsol:1:7"))                          # несуществующий вариант
        assert e.kaiten.cards[1]["properties"][SOL] == "пересоздал ссылку"


@run
async def test_quick_close_filled_card_not_asked_and_rate_closes_it():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK, properties={CAT: [501], SOL: "сделал", "id_42": "@Client_One"})
        before = len(e.alert.sent)
        await say(e, "Закрываем заявку?")
        assert len(e.alert.sent) == before                          # поля заполнены — не спрашиваем
        await say(e, "Оцените работу, пожалуйста")
        assert e.kaiten.cards[1]["column_id"] == DONE               # оценка — закрываем сами
        assert len(e.alert.sent) == before + 1 and e.alert.sent[-1].startswith("🔒")
        assert f"freopen:1:{IN_WORK}" in all_buttons(e)
        control = Control(e.relay, None, "1000")
        await control.handle(cb(f"freopen:1:{IN_WORK}"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK


@run
async def test_quick_close_rate_warns_then_closes_after_fields():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        control = Control(e.relay, None, "1000")
        await say(e, "Закрываем заявку?")
        await say(e, "Оцените, пожалуйста, нашу работу от 1 до 5")
        assert e.alert.sent[-1].startswith("⚠️ Ты отправил оценку")
        await control.handle(cb("fall:1"))
        shown = [b["callback_data"] for row in e.alert.sent_buttons[-1] for b in row]
        assert shown == ["fcat:1:501", "fcat:1:502", "fcat:1:503", "fcat:1:504", "fcat:1:506"]  # без удалённых
        await control.handle(cb("fcat:1:504"))
        assert e.kaiten.cards[1]["column_id"] == IN_WORK             # без «что сделали» не закрываем
        await control.handle(owner_msg("Разрешили доступ к камере в браузере"))
        assert e.kaiten.cards[1]["column_id"] == DONE               # заполнил после оценки — закрыли сами
        assert "Закрыл" in e.alert.sent[-1] and "freopen:1:55" in all_buttons(e)
        # чужой не может нажимать
        e.kaiten.cards[2] = card(2, column=IN_WORK)
        await control.handle(cb("fnr:2", sender=555))
        assert e.kaiten.cards[2]["column_id"] == IN_WORK


@run
async def test_quick_close_card_in_waiting_is_found_too():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=RECONTACT)
        e.kaiten.cards[5] = card(5, column=IN_WORK, responsible=999, owner=600)
        await say(e, "Оцените работу")
        assert "Категория проблемы?" in e.alert.sent[-1]


@run
async def test_quick_close_dry_run_writes_nothing():
    async with env(DRY_RUN="true") as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        control = Control(e.relay, None, "1000")
        await say(e, "Закрываем заявку?")
        await control.handle(cb("fcat:1:501"))
        await control.handle(owner_msg("что-то сделал"))
        assert not any("properties" in body for _, body in e.kaiten.patches)
        assert "[тест" in e.alert.sent[-1]


# ---------------- /learn ----------------

@run
async def test_quick_learn_next_message_becomes_trigger():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await control.handle(owner_msg("/learn wait"))
        assert "Отправь сейчас" in e.alert.sent[-1]
        text = "Хорошо, как будет время — напишите, как прошёл урок 🙌"
        await say(e, text)
        assert "Запомнил" in " ".join(e.alert.sent) and e.kaiten.cards[1]["column_id"] == WAITING
        # тот же текст в другой раз — тоже «ждём ответ»
        e.kaiten.cards[1]["column_id"] = IN_WORK
        await say(e, text)
        assert e.kaiten.cards[1]["column_id"] == WAITING
        await control.handle(owner_msg("/learn"))
        assert "как будет время" in e.alert.sent[-1]
        await control.handle(owner_msg("/learn reset"))
        e.kaiten.cards[1]["column_id"] = IN_WORK
        await say(e, text)
        assert e.kaiten.cards[1]["column_id"] == IN_WORK


@run
async def test_quick_learn_close_kind_and_only_owner():
    async with env() as e:
        control = Control(e.relay, None, "1000")
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await control.handle(owner_msg("/learn close", chat_id=555))
        assert e.relay.storage.get("learn_trigger", "") in ("", None)
        await control.handle(owner_msg("/learn close"))
        await say(e, "Всё получилось? Тогда я закрою обращение")
        assert "Категория проблемы?" in e.alert.sent[-1]
        assert e.relay.quick.kind_of("всё получилось? тогда я закрою обращение!") == "close"
        assert e.relay.quick.kind_of("ок") is None


REAL_RATE = """Пожалуйста оцените качество работы специалиста по заявке по 5-балльной шкале 😊

После завершения заявки оценку можно оставить в разделе:

«Закрытые заявки» → статус «Готово»

Для нас важно получать оценки по заявкам, чтобы видеть, где есть пробелы, улучшать качество работы техподдержки и делать процесс обработки обращений удобнее и эффективнее.

С уважением команда техподдержки школы 😊"""


@run
async def test_quick_close_real_rate_template_is_recognized():
    async with env() as e:
        assert e.relay.quick.kind_of(REAL_RATE) == "rate"
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await say(e, REAL_RATE)
        assert e.alert.sent[-1].startswith("⚠️ Ты отправил оценку")
        assert e.kaiten.cards[1]["column_id"] == IN_WORK      # поля пустые — пока не закрываем


@run
async def test_quick_wait_real_template_moves_to_waiting():
    async with env() as e:
        assert e.relay.quick.kind_of("Ждем от вас обратную связь") == "wait"
        assert e.relay.quick.kind_of("Ждём от вас обратную связь 🙏") == "wait"
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await say(e, "Ждем от вас обратную связь")
        assert e.kaiten.cards[1]["column_id"] == WAITING


NOREPLY = ("Мы не получили от Вас ответа, поэтому вынуждены закрыть заявку.\n"
           "Если проблема всё ещё актуальна — пожалуйста, оставьте заявку повторно😊")


@run
async def test_quick_noreply_fills_and_closes_with_undo():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=RECONTACT)
        e.kaiten.cards[5] = card(5, column=IN_WORK, responsible=999, owner=600)
        assert e.relay.quick.kind_of(NOREPLY) == "noreply"
        await say(e, NOREPLY)
        c = e.kaiten.cards[1]
        assert c["column_id"] == DONE and c["properties"][CAT] == [506]
        assert "не ответил" in c["properties"][SOL]
        assert e.alert.sent[-1].startswith("🔒") and "Решилось само/неактуально" in e.alert.sent[-1]
        assert f"freopen:1:{RECONTACT}" in all_buttons(e)
        control = Control(e.relay, None, "1000")
        await control.handle(cb(f"freopen:1:{RECONTACT}"))
        assert e.kaiten.cards[1]["column_id"] == RECONTACT


@run
async def test_quick_noreply_keeps_filled_fields():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK, properties={CAT: [502], SOL: "уже написал сам"})
        await say(e, NOREPLY)
        c = e.kaiten.cards[1]
        assert c["column_id"] == DONE and c["properties"][CAT] == [502] and c["properties"][SOL] == "уже написал сам"


@run
async def test_quick_noreply_without_matching_category_asks_then_close_button():
    async with env() as e:
        e.kaiten.select_values[45] = [o for o in e.kaiten.select_values[45] if o["id"] != 506]
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await say(e, NOREPLY)
        assert e.kaiten.cards[1]["column_id"] == IN_WORK
        assert "Категория проблемы?" in e.alert.sent[-1]
        assert "не ответил" in e.kaiten.cards[1]["properties"][SOL]
        control = Control(e.relay, None, "1000")
        await control.handle(cb("fcat:1:502"))                      # итог уже есть — сразу закрываем
        assert e.kaiten.cards[1]["column_id"] == DONE


@run
async def test_quick_noreply_several_cards_ask_and_dry_run_writes_nothing():
    async with env() as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        e.kaiten.cards[2] = card(2, column=IN_WORK)
        await say(e, NOREPLY)
        assert {c["column_id"] for c in e.kaiten.cards.values()} == {IN_WORK}
        assert "fnr:1" in all_buttons(e) and "fnr:2" in all_buttons(e)
        control = Control(e.relay, None, "1000")
        await control.handle(cb("fnr:2"))
        assert e.kaiten.cards[2]["column_id"] == DONE and e.kaiten.cards[1]["column_id"] == IN_WORK
    async with env(DRY_RUN="true") as e:
        e.kaiten.cards[1] = card(1, column=IN_WORK)
        await say(e, NOREPLY)
        assert e.kaiten.patches == [] and "[тест" in e.alert.sent[-1]

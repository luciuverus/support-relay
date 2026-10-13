"""Детектор инцидентов: массовые сбои, заявки коллег без ответа, завалы, руководитель и итоги дня."""
from datetime import timedelta

from app.control import Control
from app.incidents import IncidentDetector, topic_of
from tests.fakes import IN_WORK, MY_ID, NEW_COL, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

LANES = [{"id": 1, "title": "1 уровень"}, {"id": 2, "title": "2 уровень"}, {"id": 3, "title": "Внутренний"}]
COLLEAGUE = 888


def tcard(e, card_id, title, lane=1, responsible=COLLEAGUE, name="Тимур", column=NEW_COL, ago_min=0, **extra):
    created = (e.clock.now - timedelta(minutes=ago_min)).isoformat()
    data = {"id": card_id, "title": title, "column_id": column, "board_id": 900, "lane_id": lane,
            "created": created, "members": [{"id": responsible, "type": 2, "full_name": name}]}
    data.update(extra)
    e.kaiten.cards[card_id] = data
    return data


def detector(e, **kw):
    e.kaiten.board["lanes"] = LANES
    return IncidentDetector(e.cfg, e.storage, e.relay.kaiten, e.alert, e.clock, e.relay.is_paused, "Улугбек", **kw)


def with_manager(e, d, chat="555"):
    d.set_manager(chat)
    return d


# ---------------- массовый сбой ----------------

def test_incident_mass_topics_from_text():
    assert topic_of({"title": "Не проходит оплата по рассрочке"}) == "Оплаты / Битрикс"
    assert topic_of({"title": "Урок", "properties": {"id_1": "С чем возникла проблема в LiveDigital?"}}) \
        == "LiveDigital / онлайн-уроки"
    assert topic_of({"title": "Ученик не может войти в личный кабинет"}) == "Вход / доступ"
    assert topic_of({"title": "Привет"}) == ""


@run
async def test_incident_mass_three_same_topic_alert_once_then_on_doubling():
    async with env(UNTAKEN_ALERT_MIN="0", BACKLOG_LIMIT="0") as e:
        d = with_manager(e, detector(e))
        for i, lane in ((1, 1), (2, 3), (3, 1)):
            tcard(e, i, f"Не проходит оплата у ученика {i}", lane=lane, ago_min=10 - i)
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]
        mine = [t for t in e.alert.sent if "массовый сбой" in t]
        assert len(mine) == 1 and "1 уровень — 2" in mine[0] and "Внутренний — 1" in mine[0]
        assert len(d.manager_sent) == 1 and "Оплаты / Битрикс" in d.manager_sent[0]
        assert "Тимлид (Улугбек) в курсе" in d.manager_sent[0]
        # 4-я и 5-я — не повторяем
        for i in (4, 5):
            tcard(e, i, f"Оплата не проходит у клиента {i}")
            e.clock.now += timedelta(minutes=1)
            assert await d.poll() == []
        # удвоение (6) — одно обновление
        tcard(e, 6, "Не проходит оплата, деньги списались")
        assert await d.poll() == ["incident:Оплаты / Битрикс:update:6"]
        assert sum("растёт" in t for t in e.alert.sent) == 1 and len(d.manager_sent) == 2
        assert await d.poll() == []


@run
async def test_incident_mass_below_threshold_and_mixed_topics_silent():
    async with env(UNTAKEN_ALERT_MIN="0", BACKLOG_LIMIT="0") as e:
        d = with_manager(e, detector(e))
        tcard(e, 1, "Не проходит оплата")
        tcard(e, 2, "Оплата не проходит картой")        # одна проблема, но заявок только 2
        tcard(e, 3, "Не работает видео на уроке")
        tcard(e, 4, "Не могу войти")
        tcard(e, 5, "Оплата прошла вчера", ago_min=45)  # вне окна 30 минут
        assert await d.poll() == []
        assert not any("сбой" in t or "Всплеск" in t for t in e.alert.sent) and d.manager_sent == []


@run
async def test_incident_mass_spike_of_any_topics():
    async with env(UNTAKEN_ALERT_MIN="0", BACKLOG_LIMIT="0") as e:
        d = detector(e)
        for i in range(1, 9):
            tcard(e, i, f"Вопрос {i}", ago_min=i)
        assert await d.poll() == ["incident:*:new:8"]
        assert sum("Всплеск" in t for t in e.alert.sent) == 1
        assert await d.poll() == []


@run
async def test_incident_mass_counts_cards_taken_before_we_saw_them():
    async with env(UNTAKEN_ALERT_MIN="0", BACKLOG_LIMIT="0") as e:
        d = detector(e)
        tcard(e, 1, "Оплата не проходит")
        tcard(e, 2, "Оплата не проходит", column=IN_WORK, ago_min=3)   # взяли за секунды
        tcard(e, 3, "Оплата не проходит", column=IN_WORK, ago_min=4)
        tcard(e, 4, "Оплата старая", column=IN_WORK, ago_min=300)       # старая в работе — не считаем
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]


@run
async def test_incident_mass_new_episode_after_quiet_window():
    async with env(UNTAKEN_ALERT_MIN="0", BACKLOG_LIMIT="0") as e:
        d = detector(e)
        for i in (1, 2, 3):
            tcard(e, i, f"Не проходит оплата картой {i}")
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]
        e.clock.now += timedelta(hours=2)
        for i in (11, 12, 13):
            tcard(e, i, f"Не проходит оплата картой {i}")
        assert await d.poll() == ["incident:Оплаты / Битрикс:new:3"]


# ---------------- очередь команды ----------------

@run
async def test_incident_queue_colleague_ticket_after_5_min_once():
    async with env() as e:
        d = detector(e)
        tcard(e, 1, "Вопрос по расписанию", lane=2, name="Руслан")
        assert await d.poll() == []
        e.clock.now += timedelta(minutes=4)
        assert await d.poll() == []
        e.clock.now += timedelta(minutes=2)
        assert await d.poll() == ["untaken:1"]
        text = e.alert.sent[-1]
        assert "дольше 5 минут" in text and "2 уровень" in text and "Руслан" in text and "6 мин" in text
        e.clock.now += timedelta(minutes=5)
        assert await d.poll() == []


@run
async def test_incident_queue_never_for_my_own_ticket():
    async with env() as e:
        d = detector(e)
        tcard(e, 1, "Мой вопрос", responsible=MY_ID, name="Улугбек")
        e.clock.now += timedelta(minutes=20)
        assert await d.poll() == []


@run
async def test_incident_queue_outside_work_hours_waits_for_morning():
    async with env(WORK_HOURS="11:30-20:30") as e:
        d = detector(e)
        e.clock.now = e.clock.now.replace(hour=17)          # 23:00 в Бишкеке
        tcard(e, 1, "Вопрос ночью")
        e.clock.now += timedelta(minutes=30)
        assert await d.poll() == []
        e.clock.now = e.clock.now.replace(hour=5, minute=31) + timedelta(days=1)   # 11:31
        assert await d.poll() == ["untaken:1"]


@run
async def test_incident_queue_many_grouped_in_one_message_urgent_first():
    async with env() as e:
        d = detector(e)
        tcard(e, 1, "Первая")
        tcard(e, 2, "Вторая", asap=True)
        tcard(e, 3, "Третья", responsible=0, members=[])
        e.clock.now += timedelta(minutes=6)
        before = len(e.alert.sent)
        assert await d.poll() == ["untaken:3"]
        assert len(e.alert.sent) == before + 1
        text = e.alert.sent[-1]
        assert text.startswith("⏳ 3 заявки") and text.index("🔥 2") < text.index("1 «Первая»")
        assert "никто не назначен" in text


@run
async def test_incident_queue_taken_card_not_reported_and_returned_card_counts_anew():
    async with env() as e:
        d = detector(e)
        tcard(e, 1, "Вопрос")
        await d.poll()
        e.clock.now += timedelta(minutes=2)
        e.kaiten.cards[1]["column_id"] = IN_WORK
        await d.poll()
        e.clock.now += timedelta(minutes=10)
        assert await d.poll() == []
        e.kaiten.cards[1]["column_id"] = NEW_COL
        e.kaiten.cards[1]["last_moved_at"] = e.clock.now.isoformat()
        assert await d.poll() == []
        e.clock.now += timedelta(minutes=6)
        assert await d.poll() == ["untaken:1"]


@run
async def test_incident_queue_backlog_once_per_episode_to_me_and_manager():
    async with env(UNTAKEN_ALERT_MIN="0", INCIDENT_SPIKE="50") as e:
        d = with_manager(e, detector(e))
        for i in range(1, 5):
            tcard(e, i, f"Вопрос {i}", lane=1)
        tcard(e, 9, "Вопрос 9", lane=2)
        assert await d.poll() == []
        tcard(e, 5, "Вопрос 5", lane=1)
        assert await d.poll() == ["backlog:1 уровень:5"]
        assert "Завал" in e.alert.sent[-1] and "1 уровень" in d.manager_sent[-1]
        tcard(e, 6, "Вопрос 6", lane=1)
        assert await d.poll() == []
        for i in (1, 2, 3, 4):
            e.kaiten.cards[i]["column_id"] = IN_WORK
        await d.poll()                                        # осталось 2 — эпизод закончился
        for i in (7, 8, 10):
            tcard(e, i, f"Вопрос {i}", lane=1)
        assert await d.poll() == ["backlog:1 уровень:5"]


@run
async def test_incident_queue_paused_me_still_informs_manager():
    async with env(UNTAKEN_ALERT_MIN="5") as e:
        d = with_manager(e, detector(e))
        e.relay.pause(None)
        for i in (1, 2, 3):
            tcard(e, i, f"Не проходит оплата картой {i}")
        before = len(e.alert.sent)
        await d.poll()
        assert len(e.alert.sent) == before and len(d.manager_sent) == 1


# ---------------- руководитель и итоги дня ----------------

@run
async def test_incident_manager_not_set_means_nothing_sent():
    async with env() as e:
        d = detector(e)
        for i in (1, 2, 3):
            tcard(e, i, f"Не проходит оплата картой {i}")
        await d.poll()
        assert d.manager_sent == []


@run
async def test_incident_manager_from_env():
    async with env(MANAGER_CHAT_ID="4242") as e:
        d = detector(e)
        assert d.manager_chat() == "4242"
        for i in (1, 2, 3):
            tcard(e, i, f"Не проходит оплата картой {i}")
        await d.poll()
        assert len(d.manager_sent) == 1


@run
async def test_incident_manager_start_needs_bill_approval():
    async with env() as e:
        d = detector(e)
        control = Control(e.relay, None, "1000", detector=d)
        stranger = {"update_id": 5, "message": {"message_id": 3, "text": "/start",
                                                "chat": {"id": 555, "type": "private"},
                                                "from": {"id": 555, "first_name": "Руслан", "username": "rus"}}}
        await control.handle(stranger)
        assert d.manager_chat() == ""
        ask = e.alert.sent[-1]
        assert "Руслан @rus" in ask and e.alert.sent_buttons[-1][0][0]["callback_data"] == "mgr:555"
        await control.handle(stranger)                         # второй раз не спрашиваем
        assert e.alert.sent.count(ask) == 1
        # чужой не может сам себя подтвердить
        await control.handle({"update_id": 6, "callback_query": {"id": "q", "from": {"id": 555},
                                                                  "data": "mgr:555"}})
        assert d.manager_chat() == ""
        await control.handle({"update_id": 7, "callback_query": {"id": "q", "from": {"id": 1000},
                                                                  "data": "mgr:555"}})
        assert d.manager_chat() == "555" and "Здравствуйте" in d.manager_sent[-1]
        # чужой ничего не получает по командам
        before = len(e.alert.sent)
        await control.handle(owner_msg("/team", chat_id=555))
        assert len(e.alert.sent) == before
        await control.handle(owner_msg("/manager off"))
        assert d.manager_chat() == ""


@run
async def test_incident_manager_commands_team_and_digest_for_owner():
    async with env() as e:
        d = detector(e)
        control = Control(e.relay, None, "1000", detector=d)
        tcard(e, 1, "Вопрос", lane=2)
        await d.poll()
        await control.handle(owner_msg("/team"))
        assert "2 уровень: 1 заявка в очереди" in e.alert.sent[-1]
        await control.handle(owner_msg("/digest"))
        assert "Итоги дня" in e.alert.sent[-1]
        await control.handle(owner_msg("/manager"))
        assert "не подключён" in e.alert.sent[-1]


@run
async def test_incident_manager_digest_once_at_end_of_day():
    async with env(WORK_HOURS="11:30-20:30", UNTAKEN_ALERT_MIN="0") as e:
        d = with_manager(e, detector(e))
        tcard(e, 1, "Оплата не прошла", lane=1, name="Тимур")
        tcard(e, 2, "Видео не грузит", lane=2, name="Руслан")
        tcard(e, 3, "Вопрос", lane=3, name="Руслан")
        await d.poll()
        e.clock.now += timedelta(minutes=2)
        e.kaiten.cards[1]["column_id"] = IN_WORK
        await d.poll()
        e.clock.now += timedelta(minutes=10)
        e.kaiten.cards[2]["column_id"] = IN_WORK
        await d.poll()
        assert await d.digest() is False                       # ещё 12:12
        e.clock.now = e.clock.now.replace(hour=14, minute=31)  # 20:31 в Бишкеке
        assert await d.digest() is True
        assert await d.digest() is False
        text = e.alert.sent[-1]
        assert text == d.manager_sent[-1]
        assert "Новых заявок: 3" in text and "Взято за 5 минут: 50% (1 из 2)" in text
        assert "Дольше всех ждала: 2" in text and "12 мин" in text
        assert "Тимур: 1 / 2 мин" in text and "Руслан: 1 / 12 мин, позже 5 мин — 1" in text
        assert "Сейчас в очереди: 1 заявка" in text


@run
async def test_incident_manager_digest_without_people_when_disabled():
    async with env(DIGEST_BY_PERSON="false", UNTAKEN_ALERT_MIN="0") as e:
        d = detector(e)
        tcard(e, 1, "Вопрос", name="Тимур")
        await d.poll()
        e.kaiten.cards[1]["column_id"] = IN_WORK
        e.clock.now += timedelta(minutes=1)
        await d.poll()
        text = d.digest_text(d._day_start(e.clock.now), e.clock.now)
        assert "Тимур" not in text and "Взято за 5 минут: 100%" in text


@run
async def test_incident_manager_detector_off():
    async with env(INCIDENTS="false") as e:
        d = detector(e)
        for i in (1, 2, 3):
            tcard(e, i, f"Не проходит оплата картой {i}")
        assert await d.poll() == [] and await d.digest() is False

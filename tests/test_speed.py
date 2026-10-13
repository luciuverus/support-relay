"""/speed: темп сообщений людям — по умолчанию быстро, меняется из Telegram без перезапуска."""
import random
from datetime import datetime, timedelta

import pytest

from app.config import Config, ConfigError
from app.control import Control
from app.humanize import SPEEDS, typing_seconds
from app.storage import Storage
from app.transports import BusinessTransport
from tests.fakes import base_env, card, run
from tests.test_relay import env
from tests.test_v10 import owner_msg


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


def first_send_delay(e) -> float:
    """Через сколько секунд после взятия уйдёт первое сообщение."""
    msgs = e.storage.all_messages()
    first = min(datetime.fromisoformat(m["send_at"]) for m in msgs)
    return (first - e.clock.now).total_seconds()


def test_speed_fast_by_default_and_unknown_rejected():
    env_ = base_env()
    env_.pop("SPEED")
    assert Config.from_env(env_).speed == "fast"   # старый .env без SPEED сразу становится быстрым
    with pytest.raises(ConfigError):
        Config.from_env(base_env(SPEED="turbo"))


@run
async def test_speed_fast_greeting_goes_within_half_a_minute():
    async with env(SPEED="fast") as e:
        e.kaiten.cards[1] = card(1)
        assert await e.relay.handle_webhook({"data": {"id": 1}}) == "taken"
        assert 10 <= first_send_delay(e) <= 30
        e.clock.now += timedelta(seconds=51)          # второй человек — ещё через 8–20 с, не 20–70
        assert await e.relay.send_due() == 2


@run
async def test_speed_custom_keeps_env_numbers():
    async with env(SPEED="custom", HUMAN_DELAY_MIN_S="600", HUMAN_DELAY_MAX_S="600") as e:
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        assert first_send_delay(e) == 600
        assert e.relay.typing_pace() == (e.cfg.typing_cps_min, e.cfg.typing_cps_max, 45.0)


@run
async def test_speed_command_switches_live_and_only_for_owner():
    async with env(SPEED="slow") as e:
        control = Control(e.relay, None, "1000")
        await control.handle(owner_msg("/speed"))
        buttons = [b[0]["callback_data"] for b in e.alert.sent_buttons[-1]]
        assert buttons == ["speed:instant", "speed:fast", "speed:slow", "speed:custom"]
        assert "спокойно" in e.alert.sent[-1]
        await control.handle(cb("speed:instant", sender=555))       # чужой — ничего не меняется
        assert e.relay.pace().key == "slow"
        await control.handle(cb("speed:instant"))
        assert e.relay.pace().key == "instant" and "мгновенно" in e.alert.sent[-1]
        e.kaiten.cards[1] = card(1)
        await e.relay.handle_webhook({"data": {"id": 1}})
        assert first_send_delay(e) <= 5                              # без перезапуска
        await control.handle(owner_msg("/speed быстро"))
        assert e.relay.pace().key == "fast"
        await control.handle(owner_msg("/speed turbo"))
        assert e.relay.pace().key == "fast" and "Не понял" in e.alert.sent[-1]
        assert "темп сообщений" in e.relay.journal_text(None)
        assert "Темп: 🚀 быстро" in e.relay.status_text()


@run
async def test_speed_typing_follows_pace():
    async with env(SPEED="instant") as e:
        transport = BusinessTransport(None, Storage(":memory:"), random.Random(3), 4, 7)
        assert transport.typing_time("x" * 400) == 45.0              # без темпа — старые 45 с
        transport.pace = e.relay.typing_pace
        assert transport.typing_time("x" * 400) == 3.0               # мгновенно — не дольше 3 с
        e.relay.set_speed("fast")
        assert transport.typing_time("x" * 400) == 10.0
        assert 2.0 <= transport.typing_time("x" * 60) <= 5.0          # 60 знаков при 12–18 зн/с
        assert e.transport.pace == e.relay.typing_pace                # Relay сам подключает темп
        assert typing_seconds("ок", random.Random(1), 4, 7) == 2.0
        assert SPEEDS["slow"].delay_max_s == 150                      # «как раньше» не потерялось

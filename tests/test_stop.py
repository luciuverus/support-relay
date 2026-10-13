"""/stop: выключить сервис из Telegram — с подтверждением, только владельцу, код выхода 0."""
import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

from app.control import Control
from tests.fakes import FakeBotApi, FakeKaiten, base_env, run
from tests.test_relay import env
from tests.test_v10 import owner_msg

ROOT = Path(__file__).resolve().parent.parent


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


@run
async def test_stop_needs_confirmation_and_owner():
    async with env() as e:
        calls = []
        control = Control(e.relay, None, "1000", on_stop=lambda: calls.append(1))
        await control.handle(owner_msg("/stop", chat_id=555))
        await control.handle(cb("stop:yes", sender=555))
        assert calls == []
        await control.handle(owner_msg("/stop"))
        assert calls == [] and e.alert.sent_buttons[-1][0][0]["callback_data"] == "stop:yes"
        await control.handle(cb("stop:yes"))
        assert calls == [1] and "Выключаюсь" in e.alert.sent[-1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@run
async def test_stop_process_exits_with_code_zero():
    kaiten, bot = FakeKaiten(), FakeBotApi()
    kaiten_base, bot_base = await kaiten.start(), await bot.start()
    bot.updates_token = "ALERT"
    tmp = tempfile.mkdtemp()
    environ = {**os.environ, **base_env(
        KAITEN_BASE_URL=kaiten_base, TELEGRAM_API_BASE=bot_base, DB_PATH=f"{tmp}/state.db",
        ALERT_BOT_TOKEN="ALERT", HOST="127.0.0.1", PORT=str(free_port()), LOG_FILE="",
        MESSAGES_FILE=str(ROOT / "config/messages.default.json"), EMPLOYEES_FILE=f"{tmp}/none.json",
        POLL_INTERVAL_S="10", INCIDENTS="false"), "PYTHONPATH": str(ROOT)}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.main", cwd=tmp, env=environ,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        for _ in range(200):  # ждём, пока сервис начнёт читать команды
            if bot.update_requests:
                break
            await asyncio.sleep(0.05)
        assert bot.update_requests, "service never polled Telegram"
        await asyncio.sleep(0.3)
        assert proc.returncode is None                         # без подтверждения работает
        bot.updates = [cb("stop:yes") | {"update_id": 50}]
        code = await asyncio.wait_for(proc.wait(), timeout=20)
        out = (await proc.stdout.read()).decode("utf-8", "ignore")
        assert code == 0, out[-2000:]
        assert any("Выключаюсь" in p.get("text", "") for p in bot.params("sendMessage"))
        polls = len(bot.update_requests)
        await asyncio.sleep(0.5)
        assert len(bot.update_requests) == polls                # Telegram больше не опрашивается
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await kaiten.close()
        await bot.close()

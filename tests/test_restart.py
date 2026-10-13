"""/restart: перезапуск из Telegram — с подтверждением, только владельцу, код 4, лаунчеры запускают снова."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from app.control import Control
from app.main import startup_notice
from tests.fakes import FakeBotApi, FakeKaiten, base_env, run
from tests.test_relay import env
from tests.test_stop import free_port
from tests.test_v10 import owner_msg

ROOT = Path(__file__).resolve().parent.parent


def cb(data, sender=1000):
    return {"update_id": 9, "callback_query": {"id": "q", "from": {"id": sender}, "data": data}}


@run
async def test_restart_needs_confirmation_and_owner():
    async with env() as e:
        calls = []
        control = Control(e.relay, None, "1000", on_restart=lambda: calls.append(1))
        await control.handle(owner_msg("/restart", chat_id=555))
        await control.handle(cb("restart:yes", sender=555))
        assert calls == []
        await control.handle(owner_msg("/restart"))
        assert calls == [] and e.alert.sent_buttons[-1][0][0]["callback_data"] == "restart:yes"
        await control.handle(cb("restart:yes"))
        assert calls == [1] and e.storage.get("restart_requested") == "1"


@run
async def test_restart_notice_after_coming_back_even_if_recently_started():
    async with env() as e:
        await startup_notice(e.relay, e.storage, e.cfg)
        assert "Сервис запущен" in e.alert.sent[-1]
        e.storage.set("restart_requested", "1")
        await startup_notice(e.relay, e.storage, e.cfg)      # через минуту после прошлого запуска
        assert "Перезапустился" in e.alert.sent[-1] and not e.storage.get("restart_requested")


@run
async def test_restart_process_exits_with_code_four():
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
        for _ in range(200):
            if bot.update_requests:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        assert proc.returncode is None
        bot.updates = [cb("restart:yes") | {"update_id": 60}]
        code = await asyncio.wait_for(proc.wait(), timeout=20)
        out = (await proc.stdout.read()).decode("utf-8", "ignore")
        assert code == 4, out[-2000:]
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await kaiten.close()
        await bot.close()


def test_restart_launchers_start_again_on_code_four():
    bat = (ROOT / "start.bat").read_bytes().decode("utf-8")
    assert b"\n" not in (ROOT / "start.bat").read_bytes().replace(b"\r\n", b"")
    assert 'if "%CODE%"=="4" goto restart' in bat
    run_pos, crash_pos = bat.index(":run"), bat.index("timeout /t 15")
    assert bat.index('if "%CODE%"=="4" goto restart') < crash_pos                # не как падение (без 15 с)
    restart_block = bat[bat.index(":restart"):]
    assert "goto run" in restart_block and run_pos < bat.index(":restart")
    stopped_block = bat[bat.index(":stopped"):bat.index(":restart")]
    assert "exit /b 0" in stopped_block                                          # после /stop не проваливаемся
    mac = (ROOT / "start.command").read_text(encoding="utf-8")
    assert '[ "$status" -eq 4 ]' in mac and "continue" in mac.split('[ "$status" -eq 4 ]')[1].split("\n")[0]

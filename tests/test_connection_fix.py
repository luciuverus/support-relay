"""Подключение бота не теряется; напоминание при старте; клики по окну Windows не замораживают."""
import random

import aiohttp

from app.main import ENABLE_EXTENDED_FLAGS, ENABLE_QUICK_EDIT, disable_quick_edit
from app.storage import Storage
from app.transports import AlertBot, BotApi, BusinessTransport
from tests.fakes import FakeBotApi, run
from tests.test_wizard import BOT_TOKEN, LINK, Script, project, setup_fakes


@run
async def test_connection_fix_wizard_does_not_narrow_or_confirm_updates():
    kaiten, bot, host, bot_url = await setup_fakes()
    root = project()
    script = Script([host, "kaiten-token-123", LINK, BOT_TOKEN])
    from app.setup_wizard import run_wizard
    try:
        async with aiohttp.ClientSession() as session:
            await run_wizard(root, script.ask, script.say, session, kaiten_scheme="http",
                             telegram_base=bot_url, wait_start_s=2, poll_every_s=0.05)
        seen = bot.update_requests
        assert seen and all("allowed_updates" not in p and "offset" not in p for p in seen)
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_connection_fix_startup_reminder_when_not_connected():
    bot = FakeBotApi()
    base = await bot.start()
    async with aiohttp.ClientSession() as session:
        storage = Storage(":memory:")
        alert = AlertBot(None, "1")
        t = BusinessTransport(BotApi("BIZ", session, base), storage, random.Random(0), 4, 7, alert=alert)
        await t.start()
        await t.stop()
        assert "не подключён" in alert.sent[0] and "удали и добавь" in alert.sent[0]
        storage.set("business_connection_id", "BC1")
        alert.sent.clear()
        t2 = BusinessTransport(BotApi("BIZ", session, base), storage, random.Random(0), 4, 7, alert=alert)
        await t2.start()
        await t2.stop()
        assert alert.sent == []
    await bot.close()


class FakeConsole:
    def __init__(self, mode):
        self.mode = mode

    def GetStdHandle(self, handle):  # noqa: N802
        return 7

    def get_mode(self, handle):
        return self.mode

    def SetConsoleMode(self, handle, mode):  # noqa: N802
        self.mode = mode
        return 1


def test_connection_fix_windows_quick_edit_turned_off():
    console = FakeConsole(0x01F7)  # типичный режим с включённым QuickEdit
    assert disable_quick_edit("win32", console) is True
    assert not console.mode & ENABLE_QUICK_EDIT and console.mode & ENABLE_EXTENDED_FLAGS
    assert disable_quick_edit("darwin", console) is False

"""Мастер настройки: минимум вопросов, остальное находит сам, пишет рабочий .env."""
import shutil
import tempfile
from pathlib import Path

import aiohttp
import pytest

from app.config import Config
from app.setup_wizard import WizardError, parse_env, run_wizard
from tests.fakes import IN_WORK, MY_ID, NEW_COL, FakeBotApi, FakeKaiten, card, run

BOT_TOKEN = "123456789:AAH-fake_token_for_tests_abcdef"


def project() -> Path:
    root = Path(tempfile.mkdtemp())
    shutil.copy(".env.example", root / ".env.example")
    (root / "config").mkdir()
    return root


class Script:
    """Ответы пользователя по порядку; печатает всё, что сказал мастер."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.log: list[str] = []

    async def ask(self, prompt):
        self.log.append(prompt)
        if not self.answers:
            raise AssertionError(f"мастер спросил лишнее: {prompt}")
        return self.answers.pop(0)

    def say(self, text):
        self.log.append(text)


async def setup_fakes():
    kaiten, bot = FakeKaiten(), FakeBotApi()
    kaiten_url = await kaiten.start()
    bot_url = await bot.start()
    kaiten.cards[70951934] = card(70951934, board_id=900)
    bot.errors["getMe"] = {"ok": True, "result": {"id": 1, "username": "support_relay_bot"}}
    bot.updates = [{"update_id": 5, "message": {"message_id": 1, "text": "/start",
                                                "chat": {"id": 424242, "type": "private"},
                                                "from": {"id": 424242, "first_name": "Улугбек"}}}]
    host = kaiten_url.split("://", 1)[1].split("/api/latest")[0]
    return kaiten, bot, host, bot_url


LINK = "https://example.kaiten.ru/space/100001/boards/card/70951934"


@run
async def test_wizard_writes_working_env_from_few_answers():
    kaiten, bot, host, bot_url = await setup_fakes()
    root = project()
    script = Script([host, "kaiten-token-123", LINK, BOT_TOKEN])
    try:
        async with aiohttp.ClientSession() as session:
            await run_wizard(root, script.ask, script.say, session, kaiten_scheme="http",
                             telegram_base=bot_url, wait_start_s=2, poll_every_s=0.05)
        env = parse_env((root / ".env").read_text(encoding="utf-8"))
        cfg = Config.from_env(env)
        assert cfg.my_user_id == MY_ID and cfg.in_work_column_id == IN_WORK
        assert cfg.queue_column_ids == frozenset({NEW_COL})
        assert cfg.alert_chat_id == "424242" and cfg.dry_run is True and cfg.poll_interval_s == 30
        assert cfg.card_url_prefix.endswith("/space/100001/boards/card/")
        assert cfg.author_tg_field == "Ваш телеграм" and cfg.webhook_secret == ""
        assert (root / "config" / "employees.json").exists()
        assert any(p["text"].startswith("Готово") for p in bot.params("sendMessage"))
        assert "# Как сервис узнаёт" in (root / ".env").read_text(encoding="utf-8")  # комментарии на месте
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_wizard_retries_bad_token_and_bad_link():
    kaiten, bot, host, bot_url = await setup_fakes()
    root = project()
    script = Script([host, "wrong-token-xxxx", "kaiten-token-123", "https://x/card/abc", LINK, "nope", BOT_TOKEN])
    try:
        async with aiohttp.ClientSession() as session:
            await run_wizard(root, script.ask, script.say, session, kaiten_scheme="http",
                             telegram_base=bot_url, wait_start_s=2, poll_every_s=0.05)
        text = "\n".join(script.log)
        assert "Kaiten не принял ключ" in text and "Не вижу номера карточки" in text
        assert "Токен выглядит не так" in text
        assert (root / ".env").exists()
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_wizard_asks_to_pick_column_when_names_differ():
    kaiten, bot, host, bot_url = await setup_fakes()
    kaiten.board = {"id": 900, "columns": [{"id": 11, "title": "Входящие"}, {"id": 55, "title": "В работе"}]}
    root = project()
    script = Script([host, "kaiten-token-123", LINK, "1", BOT_TOKEN])
    try:
        async with aiohttp.ClientSession() as session:
            await run_wizard(root, script.ask, script.say, session, kaiten_scheme="http",
                             telegram_base=bot_url, wait_start_s=2, poll_every_s=0.05)
        assert parse_env((root / ".env").read_text(encoding="utf-8"))["QUEUE_COLUMN_IDS"] == "11"
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_wizard_gives_up_without_start_and_writes_nothing():
    kaiten, bot, host, bot_url = await setup_fakes()
    bot.updates = []
    root = project()
    script = Script([host, "kaiten-token-123", LINK, BOT_TOKEN])
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(WizardError, match="Старт"):
                await run_wizard(root, script.ask, script.say, session, kaiten_scheme="http",
                                 telegram_base=bot_url, wait_start_s=0.2, poll_every_s=0.05)
        assert not (root / ".env").exists()
    finally:
        await kaiten.close()
        await bot.close()


@run
async def test_wizard_keeps_existing_env_unless_asked():
    root = project()
    (root / ".env").write_text("DRY_RUN=false\n", encoding="utf-8")
    script = Script(["нет"])
    async with aiohttp.ClientSession() as session:
        env = await run_wizard(root, script.ask, script.say, session)
    assert env == {"DRY_RUN": "false"} and (root / ".env").read_text() == "DRY_RUN=false\n"

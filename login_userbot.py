"""Одноразовый вход в корпоративный аккаунт для TRANSPORT=userbot.

Запуск:  python login_userbot.py
Спросит номер телефона, код из Telegram и пароль 2FA (если есть) и сохранит
сессию в файл USERBOT_SESSION (по умолчанию data/corp_account.session).
Этот файл = полный доступ к аккаунту: не коммить его и не отправляй никому.
"""
import asyncio
import os
from pathlib import Path

from app.main import load_dotenv


async def main() -> None:
    from telethon import TelegramClient

    load_dotenv()
    api_id = int(os.environ["USERBOT_API_ID"])
    api_hash = os.environ["USERBOT_API_HASH"]
    session = os.environ.get("USERBOT_SESSION", "data/corp_account")
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Готово: вошли как {me.first_name} (@{me.username}). Сессия: {session}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

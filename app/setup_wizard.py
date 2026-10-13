"""Мастер настройки: задаёт пару вопросов и сам заполняет .env.

Запуск: python -m app.setup_wizard   (start.command запускает его сам при первом старте)
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from .config import Config
from .kaiten import KaitenClient, KaitenError
from .logic import column_title, flatten_columns, recontact_columns, waiting_columns
from .transports import BotApi, TelegramApiError

Ask = Callable[[str], Awaitable[str]]
Say = Callable[[str], None]

DEFAULT_DOMAIN = "example.kaiten.ru"
QUEUE_NAMES = ("очередь",)
IN_WORK_NAMES = ("в работе",)


class WizardError(RuntimeError):
    pass


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def fill_env(template: str, values: dict[str, str]) -> str:
    """Подставляет значения в строки KEY=... шаблона, сохраняя комментарии."""
    out = []
    for line in template.splitlines():
        match = re.match(r"^([A-Z0-9_]+)=", line)
        if match and match.group(1) in values:
            line = f"{match.group(1)}={values[match.group(1)]}"
        out.append(line)
    return "\n".join(out) + "\n"


def _col_title(col: dict) -> str:
    return column_title(col)


def pick_column(columns: list[dict], names: tuple[str, ...]) -> dict | None:
    for col in columns:
        if _col_title(col).casefold() in names:
            return col
    return None


async def ask_until(ask: Ask, say: Say, prompt: str, check: Callable[[str], Awaitable[object]],
                    attempts: int = 5):
    for _ in range(attempts):
        answer = (await ask(prompt)).strip()
        try:
            return answer, await check(answer)
        except WizardError as exc:
            say(f"  ✗ {exc} Попробуй ещё раз.")
    raise WizardError("Слишком много неудачных попыток — запусти мастер заново.")


async def run_wizard(root: Path, ask: Ask, say: Say, session: aiohttp.ClientSession,
                     kaiten_scheme: str = "https", telegram_base: str = "https://api.telegram.org",
                     wait_start_s: float = 300, poll_every_s: float = 2.0) -> dict[str, str]:
    env_path, example = root / ".env", root / ".env.example"
    say("Настройка support-relay. Отвечай на вопросы и нажимай Enter.\n")
    if env_path.exists():
        answer = (await ask("Файл настроек .env уже есть. Перезаписать? (да/нет) [нет]: ")).strip().lower()
        if answer not in {"да", "д", "yes", "y"}:
            say("Оставил как есть.")
            return parse_env(env_path.read_text(encoding="utf-8"))
        shutil.copy(env_path, root / ".env.bak")
        say("Старый файл сохранил как .env.bak")

    # 1. Kaiten
    domain = (await ask(f"Адрес Kaiten [{DEFAULT_DOMAIN}]: ")).strip() or DEFAULT_DOMAIN
    domain = re.sub(r"^https?://", "", domain).strip("/")
    base = f"{kaiten_scheme}://{domain}/api/latest"
    kaiten: KaitenClient | None = None
    me: dict = {}

    async def check_token(token: str):
        nonlocal kaiten, me
        if len(token) < 10:
            raise WizardError("Это не похоже на ключ API.")
        kaiten = KaitenClient(base, token, session, retries=1, backoff_s=0)
        try:
            data = await kaiten._request("GET", "/users/current")  # noqa: SLF001
        except KaitenError as exc:
            raise WizardError(f"Kaiten не принял ключ ({exc}).") from exc
        if not isinstance(data, dict) or not isinstance(data.get("id"), int):
            raise WizardError("Kaiten не принял ключ.")
        me = data
        return data

    say("\nШаг 1 из 3 — Kaiten.")
    say("Ключ API берётся в Kaiten: аватар справа сверху → профиль → раздел с API-ключами.")
    token, _ = await ask_until(ask, say, "Вставь ключ API Kaiten: ", check_token)
    say(f"  ✓ Это ты: {me.get('full_name') or me.get('username')} (id {me['id']})")

    card: dict = {}
    space_id = ""

    async def check_link(link: str):
        nonlocal card, space_id
        numbers = re.findall(r"\d{5,}", link)
        if not numbers:
            raise WizardError("Не вижу номера карточки в ссылке.")
        found = await kaiten.get_card(int(numbers[-1]))
        if not found or not found.get("board_id"):
            raise WizardError("Такую карточку не нашёл.")
        card = found
        match = re.search(r"/space/(\d+)", link)
        space_id = match.group(1) if match else str(found.get("space_id") or "")
        return found

    say("\nОткрой любую карточку на доске поддержки и скопируй ссылку из адресной строки.")
    await ask_until(ask, say, "Вставь ссылку на карточку: ", check_link)
    board = await kaiten._request("GET", f"/boards/{card['board_id']}")  # noqa: SLF001
    columns = flatten_columns(board if isinstance(board, dict) else {})
    if not columns:
        raise WizardError("Не смог прочитать колонки доски.")

    async def choose(names: tuple[str, ...], label: str) -> dict:
        found = pick_column(columns, names)
        if found:
            say(f"  ✓ Колонка «{_col_title(found)}» (id {found['id']})")
            return found
        say(f"Не нашёл колонку «{label}». Выбери номер:")
        for i, col in enumerate(columns, 1):
            say(f"  {i}. {_col_title(col)}")

        async def check(answer: str):
            if not answer.isdigit() or not 1 <= int(answer) <= len(columns):
                raise WizardError("Нужен номер из списка.")
            return columns[int(answer) - 1]

        _, col = await ask_until(ask, say, "Номер: ", check)
        return col

    queue = await choose(QUEUE_NAMES, "Очередь")
    in_work = await choose(IN_WORK_NAMES, "В работе")

    field_note = ""
    try:
        props = await kaiten.list_custom_properties()
        if not any(str(p.get("name", "")).strip().casefold() == "ваш телеграм" for p in props):
            field_note = "  ! Поле «Ваш телеграм» не нашёл — потом поправь AUTHOR_TG_FIELD в .env."
    except KaitenError:
        field_note = "  ! Не смог проверить поле «Ваш телеграм» — проверим при запуске."
    if field_note:
        say(field_note)
    else:
        say("  ✓ Поле «Ваш телеграм» на месте")

    # 2. Бот
    say("\nШаг 2 из 3 — бот.")
    say("В Telegram открой @BotFather → /newbot → придумай имя → он пришлёт токен вида 123456:ABC...")
    say("Бот должен быть НОВЫЙ, не тот, что отвечает по базе знаний.")
    bot_info: dict = {}
    api: BotApi | None = None

    async def check_bot(bot_token: str):
        nonlocal bot_info, api
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", bot_token):
            raise WizardError("Токен выглядит не так (нужно число:буквы).")
        api = BotApi(bot_token, session, telegram_base)
        try:
            bot_info = await api.call("getMe")
        except TelegramApiError as exc:
            raise WizardError(f"Telegram не принял токен ({exc}).") from exc
        return bot_info

    bot_token, _ = await ask_until(ask, say, "Вставь токен бота: ", check_bot)
    say(f"  ✓ Бот @{bot_info.get('username')}")

    # 3. chat id — ловим /start от Билла
    say(f"\nШаг 3 из 3 — со СВОЕГО личного Telegram открой t.me/{bot_info.get('username')} и нажми «Старт».")
    say("Жду…")
    chat_id = None
    waited = 0.0
    while waited <= wait_start_s and chat_id is None:
        try:
            # Без allowed_updates и без offset: ничего не подтверждаем и не отбрасываем —
            # подключение к бизнес-аккаунту потом получит сам сервис.
            updates = await api.call("getUpdates", timeout_s=40, timeout=0)
        except TelegramApiError:
            updates = []
        for update in updates or []:
            msg = update.get("message") or {}
            if str(msg.get("text", "")).startswith("/start") and (msg.get("chat") or {}).get("type") == "private":
                chat_id = msg["chat"]["id"]
                who = msg.get("from") or {}
                say(f"  ✓ Поймал: {who.get('first_name', '')} (chat id {chat_id})")
        if chat_id is None:
            await asyncio.sleep(poll_every_s)
            waited += poll_every_s
    if chat_id is None:
        raise WizardError("Не дождался «Старт» от тебя. Запусти мастер ещё раз.")
    try:
        await api.call("sendMessage", chat_id=chat_id, text="Готово! Сюда будут приходить уведомления о заявках.")
    except TelegramApiError:
        pass

    values = {
        "KAITEN_BASE_URL": base,
        "KAITEN_TOKEN": token,
        "KAITEN_CARD_URL_PREFIX": (f"{kaiten_scheme}://{domain}/space/{space_id}/boards/card/"
                                   if space_id else f"{kaiten_scheme}://{domain}/"),
        "MY_KAITEN_USER_ID": str(me["id"]),
        "IN_WORK_COLUMN_ID": str(in_work["id"]),
        "QUEUE_COLUMN_IDS": str(queue["id"]),
        "WAITING_COLUMN_IDS": ",".join(str(i) for i in waiting_columns(board if isinstance(board, dict) else {})),
        "RECONTACT_COLUMN_IDS": ",".join(str(i) for i in recontact_columns(board if isinstance(board, dict) else {})),
        "SPECIALIST_NAME": str(me.get("full_name") or "").split(" ")[0],
        "TRANSPORT": "business",
        "BUSINESS_BOT_TOKEN": bot_token,
        "ALERT_CHAT_ID": str(chat_id),
        "POLL_INTERVAL_S": "30",
        "WEBHOOK_SECRET": "",
        "HOST": "127.0.0.1",  # без вебхука снаружи ничего не нужно — и брандмауэр не спросит
        "DRY_RUN": "true",
    }
    text = fill_env(example.read_text(encoding="utf-8"), values)
    Config.from_env(parse_env(text))  # проверка: сервис примет эти настройки
    env_path.write_text(text, encoding="utf-8")
    employees = root / "config" / "employees.json"
    if not employees.exists():
        employees.write_text(json.dumps({"_comment": "Запасной вариант, если у автора пустое поле «Ваш телеграм»: "
                                         "\"почта автора\": \"@его_telegram\""}, ensure_ascii=False, indent=2)
                             + "\n", encoding="utf-8")
    say("\n✅ Настройки сохранены в .env (тестовый режим: ничего не отправляется, только уведомления тебе).")
    say(f"Последнее: подключи бота @{bot_info.get('username')} к корпоративному аккаунту —")
    say("  Настройки → Telegram для бизнеса → Чат-боты → добавь бота → разреши отвечать.")
    return values


async def _main() -> int:
    root = Path.cwd()

    async def ask(prompt: str) -> str:
        return await asyncio.to_thread(input, prompt)

    async with aiohttp.ClientSession() as session:
        try:
            await run_wizard(root, ask, print, session)
        except WizardError as exc:
            print(f"\n✗ {exc}")
            return 1
        except (KeyboardInterrupt, EOFError):
            print("\nОтменено.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))

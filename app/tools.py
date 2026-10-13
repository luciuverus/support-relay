"""Помощники для настройки (запуск в папке проекта):

  python -m app.tools chats          — с кем переписывается корпоративный аккаунт: имя, @username, chat id
  python -m app.tools card 70951934  — что сервис видит в карточке: колонка, ответственный, e-mail автора
  python -m app.tools live           — выключить тестовый режим (DRY_RUN=false) и стереть тестовые данные
  python -m app.tools test           — вернуть тестовый режим
  python -m app.tools upgrade        — после обновления: дописать в .env новые настройки
  python -m app.tools cases отчёт.xlsx — база похожих решённых заявок из выгрузки Kaiten

В Docker:  docker compose run --rm support-relay python -m app.tools chats
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys

from pathlib import Path

import aiohttp

from .config import Config
from .kaiten import KaitenClient
from .logic import card_emails, client_contact, find_author, is_responsible, normalize_people
from .storage import Storage


def _fmt_chat(chat_id, name, username, when=None) -> str:
    user = f"@{username}" if username else "(нет @username)"
    seen = f", писал {when:%d.%m %H:%M} UTC" if when else ""
    return f"{name or 'без имени'} — {user} — chat id {chat_id}{seen}"


async def chats_from_storage(storage: Storage) -> list[str]:
    rows = storage.known_chats()
    if not rows:
        return ["Пока никого: Business-бот запоминает чаты, когда в них приходит новое сообщение "
                "после запуска сервиса. Попроси коллегу написать на корпоративный аккаунт и повтори."]
    return [_fmt_chat(r["chat_id"], r["name"], r["username"], r["last_in_at"]) for r in rows]


async def chats_from_userbot(client, limit: int = 50) -> list[str]:
    lines = []
    async for dialog in client.iter_dialogs(limit=limit):
        if not getattr(dialog, "is_user", False):
            continue
        entity = dialog.entity
        lines.append(_fmt_chat(entity.id, dialog.name, getattr(entity, "username", None)))
    return lines or ["Личных чатов не найдено."]


async def describe_card(cfg: Config, kaiten: KaitenClient, people: dict[str, str], card_id: int) -> list[str]:
    card = await kaiten.get_card(card_id)
    if not card:
        return [f"Карточка {card_id} не найдена или нет доступа с этим токеном."]
    column = card.get("column_id")
    where = ("«Очередь»" if column in cfg.queue_column_ids else
             "«В работе»" if column == cfg.in_work_column_id else "другая колонка")
    lines = [
        f"«{card.get('title')}» (№{card_id} — этот номер и уйдёт в сообщении)",
        f"Колонка: {column} — {where}",
        f"Ты ответственный: {'да' if is_responsible(card, cfg.my_user_id) else 'нет'}",
    ]
    try:
        names = {f"id_{p['id']}": p.get("name", "?") for p in await kaiten.list_custom_properties()
                 if p.get("id") is not None}
    except Exception as exc:  # noqa: BLE001 - это справка, не падаем
        names = {}
        lines.append(f"(не смог получить названия полей: {exc})")
    props = card.get("properties") or {}
    if props:
        lines.append("Заполненные поля карточки:")
        for key, value in props.items():
            if value not in (None, "", [], {}):
                lines.append(f"  {names.get(key, key)} [{key}]: {value}")

    def resolve(spec: str) -> str | None:
        spec = (spec or "").strip()
        if not spec:
            return None
        direct = re.fullmatch(r"(?:id_)?(\d+)", spec)
        if direct:
            return f"id_{direct.group(1)}"
        exact = [k for k, n in names.items() if n.strip().casefold() == spec.casefold()]
        partial = [k for k, n in names.items() if spec.casefold() in n.casefold()]
        return exact[0] if exact else (partial[0] if len(partial) == 1 else None)

    author_key = resolve(cfg.author_tg_field)
    if cfg.author_tg_field and not author_key:
        lines.append(f"Поле «{cfg.author_tg_field}» в Kaiten не найдено — проверь AUTHOR_TG_FIELD.")
    contact = client_contact(card, author_key) if author_key else ""
    how = f"поле «{cfg.author_tg_field}»" if contact else ""
    if not contact:
        how_found, contact, _ = find_author(card, people, cfg.my_user_id)
        how = f"employees.json ({how_found})" if contact else ""
    emails = card_emails(card)
    if emails and not contact:
        lines.append("E-mail автора (можно вписать в employees.json как запасной вариант):")
        lines += [f"  {email}   ← {path}" for path, email in emails]
    lines.append(f"Автору напишем: {contact} (взял из: {how})" if contact else
                 f"Автору напишем: никому — поле «{cfg.author_tg_field}» пустое и в employees.json его нет")
    client_key = resolve(cfg.client_tg_field)
    client = client_contact(card, client_key) if client_key else ""
    if cfg.client_tg_field:
        lines.append(f"Клиенту напишем: {client}" if client else "Клиенту напишем: никому (поле пустое)")
    return lines


def set_mode(root: Path, live: bool) -> list[str]:
    """Переключает DRY_RUN в .env; при включении боевого режима стирает тестовые заявки и очередь.

    Чаты, которые бот уже видел, НЕ стираются — иначе Business-бот снова «забудет», кому может писать.
    """
    from .setup_wizard import fill_env

    env_path = root / ".env"
    if not env_path.exists():
        return ["Файла .env нет — сначала запусти start (мастер настройки)."]
    text = env_path.read_text(encoding="utf-8")
    if not re.search(r"^DRY_RUN=", text, re.M):
        text = text.rstrip("\n") + "\nDRY_RUN=true\n"
    env_path.write_text(fill_env(text, {"DRY_RUN": "false" if live else "true"}), encoding="utf-8")
    lines = []
    if live:
        db = root / (_env_of(text).get("DB_PATH") or "data/state.db")
        # и базы коллег (data/state-<id в Kaiten>.db) — иначе их тестовые заявки «уже взяты»
        for path in [db, *sorted(db.parent.glob("state-*.db"))]:
            if not path.exists():
                continue
            storage = Storage(str(path))
            try:
                storage.wipe_test_state()
            finally:
                storage.close()
        lines.append("✅ Боевой режим включён: сервис будет брать заявки и писать людям.")
        lines.append("Если сервис сейчас запущен — закрой его окно и запусти start заново.")
    else:
        lines.append("Тестовый режим включён: сервис только присылает тебе, что сделал бы.")
    return lines


OLD_SCHEDULE = {"WORK_HOURS": "09:00-18:00", "WORK_DAYS": "1-5"}


def upgrade_env(root: Path) -> list[str]:
    """После обновления программы: дописать в .env новые настройки, ничего не трогая из твоих.

    Исключение — старое рабочее время по умолчанию (09:00–18:00, пн–пт): его меняем на ваше,
    11:30–20:30 каждый день, если ты его не менял.
    """
    from .setup_wizard import fill_env, parse_env

    env_path, example_path = root / ".env", root / ".env.example"
    if not env_path.exists() or not example_path.exists():
        return []
    text = env_path.read_text(encoding="utf-8")
    current = parse_env(text)
    example_text = example_path.read_text(encoding="utf-8")
    example = parse_env(example_text)
    lines: list[str] = []
    if all(current.get(k) == v for k, v in OLD_SCHEDULE.items()):
        text = fill_env(text, {"WORK_HOURS": example["WORK_HOURS"], "WORK_DAYS": example["WORK_DAYS"]})
        lines.append(f"Рабочее время: {example['WORK_HOURS']}, дни {example['WORK_DAYS']}")
    missing = [key for key in example if key not in current]
    if missing:
        # берём из примера строку с комментарием над ней, чтобы было понятно, что это за настройка
        blocks = []
        example_lines = example_text.splitlines()
        for key in missing:
            idx = next(i for i, line in enumerate(example_lines) if line.startswith(f"{key}="))
            start = idx
            while start > 0 and example_lines[start - 1].startswith("#") and not example_lines[start - 1].startswith("# ---"):
                start -= 1
            blocks.append("\n".join(example_lines[start:idx + 1]))
        text = text.rstrip("\n") + "\n\n# ---------- добавлено при обновлении ----------\n" + "\n".join(blocks) + "\n"
        lines.append("Новые настройки: " + ", ".join(missing))
    if lines:
        shutil.copy(env_path, root / ".env.bak")
        env_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return lines


def _env_of(text: str) -> dict[str, str]:
    from .setup_wizard import parse_env

    return parse_env(text)


def import_cases(args: list[str], folder: Path | None = None) -> int:
    """Импорт выгрузки. Без аргумента берёт самый свежий .xlsx из папки сервиса."""
    import os

    from .similar import import_export

    folder = folder or Path.cwd()
    if args:
        source = Path(args[0].strip('"'))
    else:
        files = sorted(folder.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            print("Не нашёл выгрузку. Выгрузи из Kaiten сводный отчёт (xlsx) и перетащи файл на import_cases.bat "
                  "или положи его в папку сервиса.")
            return 1
        source = files[0]
    if not source.exists():
        print(f"Файл не найден: {source}")
        return 1
    out = os.environ.get("CASES_FILE") or "data/cases.json"
    print(f"Читаю {source.name} …")
    try:
        lines = import_export(source, folder / out if not Path(out).is_absolute() else out)
    except Exception as exc:  # файл не того формата — объясняем по-человечески
        print(f"Не получилось прочитать выгрузку: {exc}")
        return 1
    print("\n".join(lines))
    return 0


async def _main(argv: list[str]) -> int:
    from .main import load_dotenv

    load_dotenv()
    if argv and argv[0] == "upgrade":
        for line in upgrade_env(Path.cwd()):
            print("Обновил .env —", line)
        return 0
    if argv and argv[0] == "cases":
        return import_cases(argv[1:])
    if len(argv) < 1 or argv[0] not in {"chats", "card", "live", "test"} or (argv[0] == "card" and len(argv) < 2):
        print(__doc__)
        return 2
    if argv[0] in {"live", "test"}:
        print("\n".join(set_mode(Path.cwd(), argv[0] == "live")))
        return 0
    cfg = Config.from_env()
    if argv[0] == "chats":
        if cfg.transport == "userbot":
            from telethon import TelegramClient

            client = TelegramClient(cfg.userbot_session, cfg.userbot_api_id, cfg.userbot_api_hash)
            await client.connect()
            try:
                lines = await chats_from_userbot(client)
            finally:
                await client.disconnect()
        else:
            lines = await chats_from_storage(Storage(cfg.db_path))
    else:
        try:
            with open(cfg.employees_file, encoding="utf-8") as fh:
                people = normalize_people(json.load(fh))
        except FileNotFoundError:
            people = {}
        async with aiohttp.ClientSession() as session:
            lines = await describe_card(cfg, KaitenClient(cfg.kaiten_base_url, cfg.kaiten_token, session),
                                        people, int(argv[1]))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))

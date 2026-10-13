"""Бот на всю команду: один корпоративный аккаунт, один бот — несколько специалистов.

Главный специалист — тот, кто в .env (MY_KAITEN_USER_ID, ALERT_CHAT_ID). Он получает сводки, отчёты,
статистику проверок и добавляет коллег. Коллеги — в config/team.json: у каждого свои заявки (по
ответственному в Kaiten), своя смена, пауза, уведомления в личке с тем же ботом и своя база
data/state-<id в Kaiten>.db. Общие у всех: чаты корпоративного аккаунта, выученные быстрые ответы,
темп сообщений и найденные колонки доски.

Как добавить коллегу: он пишет боту /start → главному приходит кнопка «👤 Специалист поддержки» →
выбрать его имя из ответственных на доске → готово, коллега получает приветствие и начинает работать
без перезапуска.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ConfigError, _days, _hours
from .storage import Storage

log = logging.getLogger(__name__)

# kv, общие для всей команды (лежат в базе главного)
SHARED_KEYS = frozenset({
    "quick_triggers", "business_connection_id", "business_owner_id", "speed", "waiting_column_ids",
    "recontact_column_ids", "columns_discovered", "waiting_missing_alerted", "assess_wait_ids",
    "business_owner_chat", "corp_chat_warned",
})
SHARED_PREFIXES = ("last_variant:",)

WELCOME = ("👋 Готово, {name}: теперь бот работает и на тебя.\n"
           "• Заявку, назначенную на тебя в Kaiten, бот возьмёт сам и поздоровается от корпоративного аккаунта.\n"
           "• Быстрые ответы («ждём ответа», «оцените работу») сами двигают карточку.\n"
           "• Уведомления о заявках и SLA приходят сюда.\n"
           "Твоя смена: {shift}. Поменять: /shift 09:00-18:00 (или /shift 09:00-18:00 1-5 — дни недели).\n"
           "Имя в приветствии: «{name}» — поменять: /name Имя.\n"
           "Обед или выходной: /pause, /pause 1 — на час, /resume. Что происходит: /status. Все команды: /help\n"
           "Важно для KPI: пришли свой API-ключ Kaiten командой /token — тогда переносы карточек в Kaiten "
           "будут от твоего имени, а не от имени главного.")


@dataclass
class Member:
    chat_id: str
    kaiten_id: int
    name: str
    work_hours: str = ""      # пусто = WORK_HOURS из .env
    work_days: str = ""       # пусто = WORK_DAYS из .env
    kaiten_token: str = ""    # свой API-ключ Kaiten (тогда в истории карточки переносит он, а не главный)

    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        return {k: v for k, v in data.items() if v not in ("", None)}


def load_team(path: str, problems: list[str] | None = None, primary_id: int | None = None,
              primary_chat: str | None = None) -> list[Member]:
    """Коллеги из team.json. Плохую или повторную запись пропускаем (и пишем в problems), а не роняем бота."""
    problems = problems if problems is not None else []
    file = Path(path)
    if not file.exists():
        return []
    try:
        raw = json.loads(file.read_text(encoding="utf-8") or "[]")
    except ValueError as exc:
        problems.append(f"{path}: ошибка JSON — {exc}. Коллеги не подключены, пока не исправишь.")
        return []
    members: list[Member] = []
    seen_ids = {primary_id} if primary_id is not None else set()
    seen_chats = {str(primary_chat)} if primary_chat else set()
    for item in raw if isinstance(raw, list) else []:
        try:
            member = Member(chat_id=str(item["chat_id"]), kaiten_id=int(item["kaiten_id"]),
                            name=str(item.get("name") or "").strip() or "специалист",
                            work_hours=str(item.get("work_hours") or ""),
                            work_days=str(item.get("work_days") or ""),
                            kaiten_token=str(item.get("kaiten_token") or ""))
            if member.work_hours:
                _hours(member.work_hours)
            if member.work_days:
                _days(member.work_days)
        except (KeyError, TypeError, ValueError, ConfigError) as exc:
            problems.append(f"{path}: пропустил запись {item!r} — {exc}")
            continue
        if member.kaiten_id in seen_ids or member.chat_id in seen_chats:
            problems.append(f"{path}: {member.name} (Kaiten {member.kaiten_id}, чат {member.chat_id}) уже есть — "
                            "повтор пропустил")
            continue
        seen_ids.add(member.kaiten_id)
        seen_chats.add(member.chat_id)
        members.append(member)
    return members


def save_team(path: str, members: list[Member]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps([m.to_json() for m in members], ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(file)


def member_db_path(cfg: Config, member: Member) -> str:
    if cfg.db_path == ":memory:":
        return ":memory:"
    return str(Path(cfg.db_path).with_name(f"state-{member.kaiten_id}.db"))


def member_config(cfg: Config, member: Member) -> Config:
    start, end = _hours(member.work_hours) if member.work_hours else (cfg.work_start, cfg.work_end)
    days = _days(member.work_days) if member.work_days else cfg.work_days
    return dataclasses.replace(cfg, my_user_id=member.kaiten_id, alert_chat_id=member.chat_id,
                               specialist_name=member.name, work_start=start, work_end=end, work_days=days,
                               db_path=member_db_path(cfg, member),
                               kaiten_token=member.kaiten_token or cfg.kaiten_token)


def shift_text(cfg: Config) -> str:
    days = sorted(cfg.work_days)
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    if days == list(range(1, 8)):
        when = "каждый день"
    elif days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        when = f"{names[days[0] - 1]}–{names[days[-1] - 1]}"
    else:
        when = ", ".join(names[d - 1] for d in days)
    return f"{cfg.work_start:%H:%M}–{cfg.work_end:%H:%M}, {when}"


class MemberStorage(Storage):
    """База коллеги. Чаты корпоративного аккаунта и общие настройки — из базы главного."""

    def __init__(self, path: str, shared: Storage):
        super().__init__(path)
        self.shared = shared

    @staticmethod
    def _is_shared(key: str) -> bool:
        return key in SHARED_KEYS or key.startswith(SHARED_PREFIXES)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.shared.get(key, default) if self._is_shared(key) else super().get(key, default)

    def set(self, key: str, value: str) -> None:
        if self._is_shared(key):
            self.shared.set(key, value)
        else:
            super().set(key, value)

    def remember_chat(self, *args, **kwargs) -> None:
        self.shared.remember_chat(*args, **kwargs)

    def known_chats(self) -> list[dict]:
        return self.shared.known_chats()

    def find_chat(self, key: str) -> dict | None:
        return self.shared.find_chat(key)

    def _team_card_ids(self) -> set[int]:
        return self.shared._team_card_ids()  # noqa: SLF001


class Team:
    """Все специалисты одного бота: раздаёт события Telegram и Kaiten нужному специалисту."""

    def __init__(self, cfg: Config, primary, primary_control):
        self.cfg = cfg
        self.primary = primary
        self.primary_control = primary_control
        self.members: list[tuple[Member, object, object]] = []   # (участник, Relay, Control)
        self.spawn = None   # (Member) -> (Relay, Control): создать и запустить циклы (задаёт main)

    @property
    def relays(self) -> list:
        return [self.primary, *(r for _, r, _ in self.members)]

    @property
    def storages(self) -> list[Storage]:
        return [r.storage for r in self.relays]

    def member_by_chat(self, chat_id: str):
        for member, relay, control in self.members:
            if member.chat_id == str(chat_id):
                return member, relay, control
        return None

    def taken_ids(self) -> set[int]:
        return {self.cfg.my_user_id, *(m.kaiten_id for m, _, _ in self.members)}

    # --- события ---
    async def on_chat_activity(self, keys, incoming, manual, at, text: str = "") -> None:
        async def one(relay):
            try:
                await relay.on_chat_activity(keys, incoming, manual, at, text=text)
            except Exception:  # noqa: BLE001 - сбой у одного не должен ломать остальных
                log.exception("Сообщение в чате: ошибка у %s", relay.name)
        await asyncio.gather(*(one(r) for r in self.relays))

    async def handle_webhook(self, payload: dict) -> list[str]:
        results = []
        for relay in self.relays:
            try:
                results.append(await relay.handle_webhook(payload))
            except Exception:  # noqa: BLE001
                log.exception("Вебхук: ошибка у %s", relay.name)
                results.append("error")
        return results

    async def handle(self, update: dict) -> None:
        """Команды и кнопки: из чата коллеги — его боту, остальное — главному."""
        if "callback_query" in update:
            sender = str(((update["callback_query"] or {}).get("from") or {}).get("id"))
            found = self.member_by_chat(sender)
        else:
            chat = (update.get("message") or {}).get("chat") or {}
            found = self.member_by_chat(str(chat.get("id"))) if chat.get("type") == "private" else None
        control = found[2] if found else self.primary_control
        await control.handle(update)

    # --- добавить / убрать коллегу ---
    def candidates(self) -> list[tuple[int, str]]:
        """Ответственные на доске за последние дни, которых ещё нет в команде."""
        taken = self.taken_ids()
        rows = self.primary.storage._exec(  # noqa: SLF001
            "SELECT responsible_id, responsible, MAX(first_seen) AS seen FROM team_cards "
            "WHERE responsible_id IS NOT NULL AND responsible != '' GROUP BY responsible_id ORDER BY seen DESC")
        return [(int(r["responsible_id"]), str(r["responsible"])) for r in rows if int(r["responsible_id"]) not in taken]

    async def add(self, chat_id: str, kaiten_id: int, full_name: str) -> tuple[bool, str]:
        if kaiten_id in self.taken_ids():
            return False, "Этот специалист уже в команде."
        if self.member_by_chat(chat_id) or str(chat_id) == str(self.cfg.alert_chat_id):
            return False, "Этот чат уже подключён."
        name = (full_name.split()[0] if full_name.split() else "специалист")
        member = Member(chat_id=str(chat_id), kaiten_id=int(kaiten_id), name=name)
        members = [m for m, _, _ in self.members] + [member]
        save_team(self.cfg.team_file, members)
        if self.spawn is None:
            return True, f"Добавил {full_name} в {self.cfg.team_file}. Перезапусти сервис: /restart."
        relay, control = await self.spawn(member)
        self.members.append((member, relay, control))
        warning = name_warning(self.primary.messages.book, self.primary.name)
        if warning:
            await self.primary.alert.send(warning)
        self.primary.note(None, f"в команду добавлен специалист {name} (Kaiten {kaiten_id})")
        await relay.alert.send(WELCOME.format(name=name, shift=shift_text(relay.cfg)))
        return True, f"👤 Добавил {full_name}: бот уже работает и на него. Список команды: /people"

    async def remove(self, kaiten_id: int) -> bool:
        keep = [(m, r, c) for m, r, c in self.members if m.kaiten_id != kaiten_id]
        if len(keep) == len(self.members):
            return False
        gone = [(m, r) for m, r, _ in self.members if m.kaiten_id == kaiten_id]
        self.members = keep
        save_team(self.cfg.team_file, [m for m, _, _ in keep])
        for member, relay in gone:
            for task in getattr(relay, "tasks", []):
                task.cancel()
            waiting = len(relay.storage.groups_with_status("pending")) + len(relay.storage.groups_with_status("held"))
            await relay.alert.send("Бот больше не работает на тебя: главный убрал тебя из команды."
                                   + (f" Неотправленных сообщений: {waiting} — напиши этим людям сам." if waiting else ""))
            self.primary.note(None, f"из команды убран специалист {member.name} (Kaiten {member.kaiten_id})")
            self.primary.storage.set(f"mgr_asked:{member.chat_id}", "")  # сможет снова нажать /start
            relay.storage.close()
        return True

    def save_member(self, member: Member) -> None:
        save_team(self.cfg.team_file, [m for m, _, _ in self.members])

    def people_text(self) -> tuple[str, list[list[dict]]]:
        lines = [f"👥 На боте: {self.primary.name} (главный, {shift_text(self.primary.cfg)})"]
        rows = []
        for member, relay, _ in self.members:
            paused = " — на паузе" if relay.storage.get("paused_until") else ""
            key = "" if member.kaiten_token else " — без своего ключа Kaiten (/token)"
            lines.append(f"• {member.name} (Kaiten {member.kaiten_id}, {shift_text(relay.cfg)}){paused}{key}")
            rows.append([{"text": f"Убрать {member.name}", "callback_data": f"unjoin:{member.kaiten_id}"}])
        if not self.members:
            lines.append("Коллег пока нет. Пусть коллега напишет этому боту /start — пришлю кнопку.")
        return "\n".join(lines), rows


def hardcoded_name(book, name: str) -> list[str]:
    """Разделы messages.json, где имя главного написано текстом, а не {name}: коллеги здоровались бы им."""
    if not name or len(name) < 3:
        return []
    low = name.casefold()
    kinds = []
    for kind, variants in (getattr(book, "variants", {}) or {}).items():
        if any(low in bubble.casefold() for variant in variants for bubble in variant):
            kinds.append(kind)
    return kinds


def name_warning(book, name: str) -> str:
    kinds = hardcoded_name(book, name)
    if not kinds:
        return ""
    return (f"⚠️ В config/messages.json имя «{name}» написано текстом (разделы: {', '.join(kinds)}). Коллеги будут "
            f"здороваться твоим именем. Открой файл, замени «{name}» на {{name}} (с фигурными скобками) и сохрани — "
            "бот подхватит сам, перезапуск не нужен.")


def register_secret(secret: str) -> None:
    """Новый ключ (коллега прислал свой) — тоже вычищать из журнала."""
    if not secret or len(secret) < 6:
        return
    for handler in logging.getLogger().handlers:
        for flt in handler.filters:
            if hasattr(flt, "secrets") and secret not in flt.secrets:
                flt.secrets.append(secret)


def build_member(member: Member, *, cfg: Config, primary, transport, alert_api=None, connected=None,
                 detector=None, team=None, rng=None):
    """Relay и Control коллеги: своя база и уведомления, общие с главным транспорт, тексты и Kaiten."""
    from .control import Control
    from .kaiten import KaitenClient
    from .reports import Reporter
    from .service import Relay
    from .transports import AlertBot

    register_secret(member.kaiten_token)
    mcfg = member_config(cfg, member)
    storage = MemberStorage(mcfg.db_path, primary.storage)
    kaiten = (KaitenClient(mcfg.kaiten_base_url, mcfg.kaiten_token, primary.kaiten.session)
              if member.kaiten_token else primary.kaiten)
    relay = Relay(mcfg, storage, kaiten, transport, AlertBot(alert_api, member.chat_id), primary.messages,
                  primary.employees, rng, primary.clock)
    relay.is_primary = False
    control = Control(relay, alert_api, member.chat_id, connected, detector, reporter=Reporter(relay, None),
                      team=team, admin=False, member=member)
    return relay, control


def parse_shift(arg: str) -> tuple[str, str]:
    """«09:00-18:00» или «09:00-18:00 1-5» → (часы, дни). ConfigError, если не так."""
    parts = arg.replace("–", "-").replace("—", "-").split()
    if not parts:
        raise ConfigError("Пример: /shift 09:00-18:00 или /shift 09:00-18:00 1-5")
    hours = re.sub(r"(?<!\d)(\d):", r"0\1:", parts[0])  # 9:00 → 09:00
    _hours(hours)
    days = parts[1] if len(parts) > 1 else ""
    if days:
        _days(days)
    if not re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", hours):
        raise ConfigError("Пример: /shift 09:00-18:00")
    return hours, days

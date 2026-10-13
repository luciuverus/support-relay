"""Точка входа: HTTP-сервер для вебхука Kaiten + фоновые циклы отправки и SLA."""
from __future__ import annotations

import asyncio
import hmac
import logging
import logging.handlers
import os
import random
import re
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

from .config import Config
from .humanize import MessageStore
from .kaiten import KaitenClient
from .service import Relay, load_employees
from .storage import Storage
from .control import Control
from .incidents import IncidentDetector
from .reports import Reporter
from .team import Team, build_member, load_team
from .transports import AlertBot, BotApi, BusinessTransport, Transport, UpdatePoller, UserbotTransport

log = logging.getLogger("support-relay")

SESSION = web.AppKey("session", aiohttp.ClientSession)
STORAGE = web.AppKey("storage", Storage)
TRANSPORT = web.AppKey("transport", Transport)
RELAY = web.AppKey("relay", Relay)
TASKS = web.AppKey("tasks", set)
LOOPS = web.AppKey("loops", list)
STOP = web.AppKey("stop", asyncio.Event)  # /stop из Telegram: выключить сервис целиком
RESTART = web.AppKey("restart", list)       # /restart: [True], если остановка — ради перезапуска
RESTART_EXIT = 4  # start.bat / start.command запускают сервис заново сразу
POLLERS = web.AppKey("pollers", list)
TEAM = web.AppKey("team", Team)

ALREADY_RUNNING_EXIT = 3
CLOUD_MARKERS = ("onedrive", "google drive", "googledrive", "dropbox", "yandex.disk", "yandexdisk",
                 "яндекс.диск", "icloud")

SEND_INTERVAL_S = 5
SLA_INTERVAL_S = 30
TEAM_INTERVAL_S = 60  # детектор инцидентов: вся доска раз в минуту


def build_transport(cfg: Config, session: aiohttp.ClientSession, storage: Storage,
                    rng: random.Random, alert: AlertBot) -> Transport:
    if cfg.transport == "business":
        api = BotApi(cfg.business_bot_token, session, cfg.telegram_api_base)
        return BusinessTransport(api, storage, rng, cfg.typing_cps_min, cfg.typing_cps_max, alert=alert)
    from telethon import TelegramClient  # только для userbot-режима

    client = TelegramClient(cfg.userbot_session, cfg.userbot_api_id, cfg.userbot_api_hash)
    return UserbotTransport(client, rng, cfg.typing_cps_min, cfg.typing_cps_max)


async def _loop(name: str, interval: float, func) -> None:
    while True:
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - фоновые циклы не должны падать
            log.exception("Ошибка в цикле %s", name)
        await asyncio.sleep(interval)


async def _safe(coro) -> None:
    try:
        result = await coro
        log.info("webhook: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки вебхука")


def create_app(cfg: Config) -> web.Application:
    app = web.Application()
    app[TASKS] = set()
    app[LOOPS] = []
    app[STOP] = asyncio.Event()
    app[RESTART] = []

    async def on_startup(app: web.Application) -> None:
        session = aiohttp.ClientSession()
        storage = Storage(cfg.db_path)
        rng = random.Random()
        alert_api = BotApi(cfg.alert_bot_token, session, cfg.telegram_api_base)
        alert = AlertBot(alert_api, cfg.alert_chat_id)
        transport = build_transport(cfg, session, storage, rng, alert)
        default_texts = Path(cfg.messages_file).with_name("messages.default.json")
        relay = Relay(
            cfg, storage, KaitenClient(cfg.kaiten_base_url, cfg.kaiten_token, session), transport,
            alert, MessageStore(cfg.messages_file, rng, default_texts), load_employees(cfg.employees_file), rng,
        )
        await relay.resolve_name()
        connected = (lambda: bool(storage.get("business_connection_id"))) if cfg.transport == "business" else None
        detector = IncidentDetector(cfg, storage, relay.kaiten, alert, is_paused=relay.is_paused, name=relay.name)
        reporter = Reporter(relay, detector if cfg.incidents else None)
        control = Control(relay, alert_api, cfg.alert_chat_id, connected, detector, on_stop=app[STOP].set,
                          reporter=reporter,
                          on_restart=lambda: (app[RESTART].append(True), app[STOP].set()))
        # вся команда на одном боте: коллеги из config/team.json (добавляются кнопкой без перезапуска)
        team = Team(cfg, relay, control)
        control.team, reporter.team = team, team
        relay.kaiten.list_ttl_s = 3.0  # несколько специалистов опрашивают одни колонки — не дёргаем Kaiten зря

        def start_loops(r, extra=None) -> list:
            async def sla_and_heartbeat():
                await r.check_sla()
                await r.heartbeat()
                r.prune_journal()
                if extra:
                    await extra()

            async def poll_all():
                await r.poll_queue()
                await r.poll_waiting()
                await r.poll_wait_hints()
                await r.assess.poll()

            who = "" if r.is_primary else f":{r.cfg.my_user_id}"
            tasks = [asyncio.create_task(_loop("send" + who, SEND_INTERVAL_S, r.send_due)),
                     asyncio.create_task(_loop("sla" + who, SLA_INTERVAL_S, sla_and_heartbeat))]
            if cfg.poll_interval_s > 0:
                tasks.append(asyncio.create_task(_loop("poll" + who, cfg.poll_interval_s, poll_all)))
            r.tasks = tasks
            app[LOOPS].extend(tasks)
            return tasks

        async def spawn(member):
            mrelay, mcontrol = build_member(member, cfg=cfg, primary=relay, transport=transport,
                                            alert_api=alert_api, connected=connected, detector=detector,
                                            team=team, rng=rng)
            if member.kaiten_token and not await key_works(mrelay, member):
                mrelay.set_kaiten(relay.kaiten)  # свой ключ не работает — пока работаем ключом главного
                await mrelay.alert.send("⚠️ Твой ключ Kaiten не работает (отозван или неверный). Пока переношу "
                                        "карточки ключом главного. Пришли новый: /token КЛЮЧ.")
                await relay.alert.send(f"⚠️ У {member.name} не работает свой ключ Kaiten — работаю твоим.")
            start_loops(mrelay)
            return mrelay, mcontrol

        team.spawn = spawn
        # коллег — до приёма сообщений из Telegram: иначе их команды и события, пришедшие, пока бот
        # был выключен, достанутся главному
        problems: list[str] = []
        for member in load_team(cfg.team_file, problems, cfg.my_user_id, cfg.alert_chat_id):
            try:
                mrelay, mcontrol = await spawn(member)
            except Exception as exc:  # noqa: BLE001 - один коллега не должен ронять весь бот
                log.exception("Не смог подключить %s", member.name)
                problems.append(f"{member.name}: не подключил — {exc}")
                continue
            team.members.append((member, mrelay, mcontrol))
        for problem in problems:
            await relay.alert.send(f"⚠️ Команда на боте: {problem}")
        if team.members:
            from .team import name_warning
            warning = name_warning(relay.messages.book, relay.name)
            if warning:
                await relay.alert.send(warning)
        if team.members:
            log.info("Специалисты на боте: %s, %s", relay.name, ", ".join(m.name for m, _, _ in team.members))

        app[POLLERS] = []
        if isinstance(transport, BusinessTransport):
            transport.on_activity = team.on_chat_activity
        if isinstance(transport, BusinessTransport) and cfg.alert_bot_token == cfg.business_bot_token:
            transport.extra_handler = team.handle  # один бот: и корпоративный аккаунт, и команды специалистов
        else:
            poller = UpdatePoller(alert_api, storage, "tg_offset_alert", ["message", "callback_query"],
                                  team.handle)
            poller.start()
            app[POLLERS].append(poller)
        await transport.start()
        await control.setup_menu()
        app[SESSION], app[STORAGE], app[TRANSPORT], app[RELAY], app[TEAM] = session, storage, transport, relay, team
        start_loops(relay, reporter.tick)
        from .lessonstats import SCAN_INTERVAL_S
        app[LOOPS].append(asyncio.create_task(_loop("lessons", SCAN_INTERVAL_S, relay.lessons.scan)))
        if cfg.incidents:
            async def team_loop():
                await detector.poll()
                await detector.digest()
            app[LOOPS].append(asyncio.create_task(_loop("team", TEAM_INTERVAL_S, team_loop)))
        await startup_notice(relay, storage, cfg)
        log.info("support-relay запущен: транспорт=%s, опрос Kaiten=%s, вебхук=%s, dry_run=%s",
                 transport.name, f"каждые {cfg.poll_interval_s} с" if cfg.poll_interval_s else "выкл",
                 "вкл" if cfg.webhook_secret else "выкл", cfg.dry_run)

    async def on_cleanup(app: web.Application) -> None:
        tasks = [*app[LOOPS], *app[TASKS]]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for poller in app.get(POLLERS, []):
            await poller.stop()
        if TRANSPORT in app:
            await app[TRANSPORT].stop()
        if SESSION in app:
            await app[SESSION].close()
        if TEAM in app:
            for _, mrelay, _ in app[TEAM].members:
                mrelay.storage.close()
        if STORAGE in app:
            app[STORAGE].close()

    async def kaiten_webhook(request: web.Request) -> web.Response:
        if not cfg.webhook_secret or not hmac.compare_digest(request.match_info["secret"], cfg.webhook_secret):
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        # Отвечаем сразу, обработка — в фоне (Kaiten не должен ждать Telegram).
        task = asyncio.create_task(_safe(request.app[TEAM].handle_webhook(payload)))
        request.app[TASKS].add(task)
        task.add_done_callback(request.app[TASKS].discard)
        return web.json_response({"ok": True})

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "transport": cfg.transport})

    app.router.add_post("/kaiten/{secret}", kaiten_webhook)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def key_works(relay: Relay, member) -> bool:
    """Свой ключ Kaiten коллеги принадлежит ему и работает."""
    from .kaiten import KaitenError
    try:
        me = await relay.kaiten.current_user() or {}
    except KaitenError:
        return False
    return me.get("id") == member.kaiten_id


async def startup_notice(relay: Relay, storage: Storage, cfg: Config) -> None:
    """Короткое «запустился» (не чаще раза в 10 минут — на случай перезапусков подряд)."""
    now = relay.clock()
    if storage.get("restart_requested"):
        storage.set("restart_requested", "")
        storage.set("startup_notice_at", now.isoformat())
        relay.note(None, "перезапустился по команде /restart")
        await relay.alert.send("✅ Перезапустился, снова работаю. /status — подробности.")
        return
    last = storage.get("startup_notice_at")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=10):
        return
    storage.set("startup_notice_at", now.isoformat())
    notes = []
    if in_cloud_folder(os.getcwd()):
        notes.append("⚠️ Папка программы лежит в облачной папке (OneDrive/Google Drive/…). Там же ключи "
                     "Kaiten и бота — перенеси папку, например, в C:\\support-relay.")
    paused = " (на паузе — /resume)" if storage.get("paused_until") else ""
    await relay.alert.send(f"🟢 Сервис запущен{paused}. /status — подробности." + ("\n" + "\n".join(notes) if notes else ""))


def in_cloud_folder(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    return any(marker in low for marker in CLOUD_MARKERS)


class SecretFilter(logging.Filter):
    """Вычищает токены и ключи из журнала: журнал можно спокойно показать кому-то для разбора."""

    BOT_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = self.BOT_TOKEN.sub("***", message)
        for secret in self.secrets:
            clean = clean.replace(secret, "***")
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            # текст ошибки (traceback) тоже может содержать ключ — чистим и его
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        if record.stack_info:
            record.stack_info = self._clean(record.stack_info)
        return True

    def _clean(self, text: str) -> str:
        text = self.BOT_TOKEN.sub("***", text)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text


def setup_logging(cfg: Config | None = None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg and cfg.log_file:
        Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=1_000_000, backupCount=3,
                                                             encoding="utf-8"))
    secrets = [cfg.kaiten_token, cfg.business_bot_token, cfg.alert_bot_token, cfg.userbot_api_hash] if cfg else []
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(SecretFilter(secrets))
        root.addHandler(handler)


def port_is_free(host: str, port: int) -> bool:
    """Второй экземпляр сервиса не должен стартовать: иначе двойные сообщения людям."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform.startswith("win"):
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        try:
            sock.bind((host if host not in ("", "0.0.0.0") else "0.0.0.0", port))
        except OSError:
            return False
    return True


def load_dotenv(path: str = ".env") -> None:
    """Простая загрузка .env без зависимостей (переменные окружения важнее файла)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(platform: str | None = None, kernel32=None) -> bool:
    """Windows: не давать компьютеру уснуть, пока работает сервис (прав администратора не нужно).

    На Mac то же делает caffeinate в start.command. Возвращает True, если запрет сна включён.
    """
    platform = platform or sys.platform
    if not platform.startswith("win"):
        return False
    try:
        if kernel32 is None:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return bool(kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception:  # noqa: BLE001 - не критично
        log.warning("Не смог запретить Windows уходить в сон — проверь настройки электропитания")
        return False


ENABLE_QUICK_EDIT = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10


def disable_quick_edit(platform: str | None = None, kernel32=None) -> bool:
    """Windows: выключить режим «Выбрать» в окне консоли.

    Иначе случайный клик по окну ставит программу на паузу, и заявки перестают браться.
    """
    platform = platform or sys.platform
    if not platform.startswith("win"):
        return False
    try:
        if kernel32 is None:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            mode = ctypes.c_uint32()
            handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            current = mode.value
        else:
            handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            current = kernel32.get_mode(handle)
        new_mode = (current | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:  # noqa: BLE001 - не критично
        return False


def main() -> None:
    setup_logging()
    load_dotenv()
    cfg = Config.from_env()
    setup_logging(cfg)
    if not port_is_free(cfg.host, cfg.port):
        log.error("Сервис уже запущен в другом окне (порт %s занят). Второй экземпляр не запускаю.", cfg.port)
        sys.exit(ALREADY_RUNNING_EXIT)
    if keep_awake():
        log.info("Windows не будет уходить в сон, пока сервис работает")
    if disable_quick_edit():
        log.info("Клики по окну больше не ставят сервис на паузу")
    app = create_app(cfg)
    try:
        asyncio.run(serve(app, cfg.host, cfg.port))
    except KeyboardInterrupt:
        pass  # Ctrl+C — обычная остановка
    if app[RESTART]:
        log.info("Перезапуск по команде /restart")
        sys.exit(RESTART_EXIT)
    log.info("Сервис остановлен")
    sys.exit(0)  # код 0: start.bat не перезапускает


async def serve(app: web.Application, host: str, port: int) -> None:
    """Работать, пока не придёт /stop (или Ctrl+C)."""
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
        await app[STOP].wait()
        log.info("Получена команда /stop или /restart — выключаюсь")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()

"""Команды тебе в личке с ботом уведомлений: /status, /pause, /resume, и кнопка «Отправил вручную»."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from .humanize import SPEED_KEYS, SPEEDS
from .transports import BotApi, TelegramApiError

log = logging.getLogger(__name__)

# Основное — то, чем пользуешься каждый день. Остальное — /help all (настройка и отладка).
HELP = ("Команды:\n"
        "/work 70951934 — взять заявку в работу после оценки (кнопка «▶️ В работу» делает то же)\n"
        "/status — что сейчас с сервисом\n"
        "/pause — пауза до /resume (обед, отпуск); /pause 2 — на 2 часа\n"
        "/resume — снова брать заявки\n"
        "/report — отчёт за неделю; /report month — за месяц\n"
        "/checks — проверки перед уроком (lesson-check); /checks csv — таблица\n"
        "/similar текст — похожие решённые заявки и что тогда помогло\n"
        "/help all — остальные команды (настройка и отладка)")

HELP_ALL = (HELP.replace("\n/help all — остальные команды (настройка и отладка)", "") + "\n\nНастройка и отладка:\n"
            "/speed — как быстро писать людям (мгновенно / быстро / спокойно)\n"
            "/learn wait | close | rate | noreply — научить бота твоим быстрым ответам; /learn — список\n"
            "/log — последние действия бота; /log 70951934 — история заявки\n"
            "/team — что сейчас в очереди у всей команды; /digest — итоги дня сейчас\n"
            "/manager — кто получает сводки для руководителя; /manager off — отключить\n"
            "/people — кто из коллег на боте (добавить: коллега пишет боту /start)\n"
            "/restart — перезапустить сервис; /stop — выключить полностью")

# Ключи API выглядят как одна длинная «строка-пароль» без пробелов: буквы и цифры вперемешку
KEY_LIKE = re.compile(r"(?=[^\s]*\d)(?=[^\s]*[A-Za-z])[A-Za-z0-9_\-.]{24,}")

# Команды только для главного специалиста (того, кто в .env)
ADMIN_ONLY = frozenset({"stop", "restart", "manager", "people", "команда", "learn"})

HELP_MEMBER_ALL = (HELP.replace("\n/help all — остальные команды (настройка и отладка)", "") + "\n\nЕщё:\n"
                   "/shift 09:00-18:00 — твоя смена (можно с днями: /shift 09:00-18:00 1-5)\n"
                   "/speed — как быстро писать людям (общий для всей команды)\n"
                   "/log — последние действия бота; /log 70951934 — история заявки\n"
                   "/team — очередь всей команды; /digest — итоги дня")

COMMANDS = [
    {"command": "work", "description": "В работу после оценки: /work номер"},
    {"command": "status", "description": "Что сейчас с сервисом"},
    {"command": "pause", "description": "Пауза (можно /pause 2 — на 2 часа)"},
    {"command": "resume", "description": "Снова брать заявки"},
    {"command": "report", "description": "Отчёт: /report, /report month"},
    {"command": "checks", "description": "Проверки перед уроком: /checks, /checks csv"},
    {"command": "similar", "description": "Похожие решённые заявки: /similar текст"},
    {"command": "help", "description": "Команды; /help all — все"},
]

WELCOME_MANAGER = ("Здравствуйте! Сюда будут приходить сообщения от техподдержки: массовые сбои, "
                   "завалы в очереди и итоги дня.")


class Control:
    def __init__(self, relay, api: BotApi | None, owner_chat_id: str, connected=None, detector=None,
                 on_stop=None, on_restart=None, reporter=None, team=None, admin: bool = True, member=None):
        self.relay = relay
        self.team = team      # вся команда на боте (Team) — у главного: добавить/убрать коллег
        self.admin = admin    # главный: /stop, /restart, /manager, /people; у коллеги — нет
        self.member = member  # коллега (Member) — для /shift
        self.reporter = reporter  # отчёт за неделю/месяц (/report)
        self.on_stop = on_stop  # () -> None: выключить сервис целиком (/stop)
        self.on_restart = on_restart  # () -> None: перезапустить сервис (/restart)
        self.detector = detector  # детектор инцидентов (/team, /digest, руководитель)
        self.api = api
        self.owner = str(owner_chat_id)
        self.connected = connected  # функция () -> bool | None: подключён ли Business-бот

    async def setup_menu(self) -> None:
        if self.api is None:
            return
        try:
            await self.api.call("setMyCommands", commands=COMMANDS)
        except TelegramApiError as exc:
            log.debug("setMyCommands: %s", exc)

    async def reply(self, text: str) -> None:
        await self.relay.alert.send(text)

    async def handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != self.owner or chat.get("type") != "private":
            await self._stranger(msg)  # чужим не отвечаем; на /start спросим тебя, не руководитель ли это
            return
        text = str(msg.get("text") or "").strip()
        match = re.match(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", text, re.S)
        if not match:
            if text and KEY_LIKE.fullmatch(text):
                # похоже на API-ключ без /token — в поле карточки и журнал его не пишем
                if self.api is not None and msg.get("message_id"):
                    try:
                        await self.api.call("deleteMessage", chat_id=self.owner, message_id=msg["message_id"])
                    except TelegramApiError as exc:
                        log.debug("deleteMessage: %s", exc)
                await self.reply("Похоже на ключ Kaiten — никуда его не записал и сообщение удалил. "
                                 "Чтобы подключить свой ключ, пришли: /token КЛЮЧ")
                return
            if text:
                await self.relay.quick.free_text(text)  # ответ на «что сделали по заявке?»
            return
        command, arg = match.group(1).lower(), (match.group(2) or "").strip()
        if not self.admin and command in ADMIN_ONLY:
            await self.reply("Эту команду выполняет главный специалист. Тебе доступно: /help")
            return
        if command == "status":
            await self.reply(self.relay.status_text(self.connected() if self.connected else None))
        elif command == "pause":
            hours = None
            if arg:
                try:
                    hours = float(arg.replace(",", "."))
                except ValueError:
                    await self.reply("Не понял. Пример: /pause 2 — пауза на 2 часа, /pause — до /resume.")
                    return
                if not 0 < hours <= 24 * 30:
                    await self.reply("Пауза от нескольких минут до 30 дней, например /pause 0.5 или /pause 48.")
                    return
            await self.reply(self.relay.pause(hours))
        elif command == "resume":
            await self.reply(self.relay.resume())
        elif command in {"work", "inwork", "вработу", "в_работу", "работа"}:
            await self._work(arg)
        elif command in {"help", "помощь", "start"} and arg.lower() in {"all", "все", "всё"}:
            await self.reply(HELP_ALL if self.admin else HELP_MEMBER_ALL)
        elif command in {"shift", "смена"}:
            await self._shift(arg)
        elif command in {"name", "имя"}:
            await self._name(arg)
        elif command in {"token", "ключ"}:
            await self._token(arg, msg)
        elif command in {"people", "команда"} and self.team is not None:
            text, rows = self.team.people_text()
            await self.relay.alert.send(text, rows or None)
        elif command == "report":
            if self.reporter is None:
                from .reports import Reporter
                self.reporter = Reporter(self.relay, self.detector)
            words = arg.lower().split()
            kind = "month" if "month" in words or "месяц" in words else "week"
            previous = "last" in words or "прошл" in arg.lower()
            today = self.relay.clock().astimezone(self.relay.cfg.tz).date()
            await self.reply(self.reporter.text(self.reporter.period(kind, today, previous=previous)))
        elif command in {"checks", "проверки"}:
            await self._checks(arg.lower())
        elif command in {"speed", "скорость"}:
            key = {"мгновенно": "instant", "быстро": "fast", "медленно": "slow", "спокойно": "slow",
                   "env": "custom"}.get(arg.lower(), arg.lower())
            if key:
                await self.reply(self.relay.set_speed(key))
            else:
                await self.relay.alert.send(
                    f"Сейчас темп — {self.relay.pace().describe()}.\nВыбери новый:",
                    [[{"text": SPEEDS[k].label, "callback_data": f"speed:{k}"}] for k in SPEEDS]
                    + [[{"text": "⚙️ как в .env", "callback_data": "speed:custom"}]])
        elif command == "log":
            number = re.sub(r"\D", "", arg)
            await self.reply(self.relay.journal_text(int(number) if number else None))
        elif command == "restart":
            await self.relay.alert.send("🔄 Перезапустить сервис? Он выключится и через несколько секунд "
                                        "включится снова (если запущен через start.bat).",
                                        [[{"text": "🔄 Да, перезапустить", "callback_data": "restart:yes"}]])
        elif command == "stop":
            await self.relay.alert.send(
                "⛔ Выключить сервис полностью? Заявки не будут браться, пока ты не запустишь start.bat "
                "на компьютере. Для выходного лучше /pause — его можно снять отсюда через /resume.",
                [[{"text": "⛔ Да, выключить", "callback_data": "stop:yes"}]])
        elif command == "learn":
            kind = arg.lower().split()[0] if arg else ""
            quick = self.relay.quick
            if kind in {"wait", "close", "rate", "noreply"}:
                await self.reply(quick.start_learning(kind))
            elif kind == "reset":
                await self.reply(quick.reset_learned())
            else:
                await self.reply(quick.list_text())
        elif command == "similar":
            await self.reply(self._similar(arg))
        elif command == "team" and self.detector:
            await self.reply(self.detector.snapshot())
        elif command == "digest" and self.detector:
            now = self.detector.clock()
            await self.reply(self.detector.digest_text(self.detector._day_start(now), now))
        elif command == "manager" and self.detector:
            if arg.lower() in {"off", "нет", "выкл"}:
                self.detector.storage.set("manager_chat_id", "")
                left = self.detector.manager_chat()
                await self.reply("Сводки руководителю отключены." if not left else
                                 f"Убрал, но в .env задан MANAGER_CHAT_ID={left} — удали его там.")
            else:
                chat_id = self.detector.manager_chat()
                await self.reply(f"Сводки руководителю идут в чат {chat_id}." if chat_id else
                                 "Руководитель не подключён. Пусть он напишет этому боту /start — "
                                 "я пришлю тебе кнопку, чтобы подтвердить.")
        else:
            await self.reply(HELP)

    async def _name(self, arg: str) -> None:
        if self.member is None:
            await self.reply("Твоё имя в приветствии задаётся в .env (SPECIALIST_NAME).")
            return
        name = arg.strip()
        if not name or len(name) > 40:
            await self.reply(f"Сейчас в приветствии: «{self.relay.name}». Поменять: /name Тимур")
            return
        self.member.name = name
        self.relay.name = name
        self.relay.cfg.specialist_name = name
        if self.team is not None:
            self.team.save_member(self.member)
        await self.reply(f"✅ В приветствии теперь: «На связи технический специалист - {name}».")

    async def _token(self, arg: str, msg: dict) -> None:
        from .kaiten import KaitenClient, KaitenError
        from .team import register_secret

        if self.member is None:
            await self.reply("Ключ Kaiten главного специалиста лежит в .env (KAITEN_TOKEN).")
            return
        if not arg:
            await self.reply("Kaiten → профиль → API-ключи → «Создать». Скопируй ключ и пришли сюда: /token КЛЮЧ. "
                             "Сообщение с ключом я сразу удалю. Нужно для KPI: тогда переносы карточек в истории "
                             "Kaiten — от твоего имени.")
            return
        key = arg.split()[0]
        if self.api is not None and msg.get("message_id"):
            try:  # ключ не должен висеть в переписке
                await self.api.call("deleteMessage", chat_id=self.owner, message_id=msg["message_id"])
            except TelegramApiError as exc:
                log.debug("deleteMessage: %s", exc)
        client = KaitenClient(self.relay.cfg.kaiten_base_url, key, self.relay.kaiten.session)
        try:
            me = await client.current_user() or {}
        except KaitenError as exc:
            await self.reply(f"Kaiten не принял ключ: {exc}. Проверь и пришли ещё раз.")
            return
        if me.get("id") != self.member.kaiten_id:
            await self.reply("Это ключ другого пользователя Kaiten — нужен твой собственный.")
            return
        register_secret(key)
        self.member.kaiten_token = key
        self.relay.cfg.kaiten_token = key
        self.relay.set_kaiten(client)
        if self.team is not None:
            self.team.save_member(self.member)
        self.relay.note(None, "подключён свой ключ Kaiten")
        await self.reply("✅ Ключ подключён: теперь в Kaiten карточки переношу от твоего имени. Сообщение с ключом удалил.")

    async def _shift(self, arg: str) -> None:
        from .config import ConfigError, _days, _hours
        from .team import parse_shift, shift_text

        if self.member is None:
            await self.reply(f"Твоя смена: {shift_text(self.relay.cfg)}. Она задаётся в .env (WORK_HOURS, "
                             "WORK_DAYS); /shift меняет смену коллегам — каждый у себя.")
            return
        if not arg:
            await self.reply(f"Твоя смена: {shift_text(self.relay.cfg)}. Поменять: /shift 09:00-18:00 "
                             "или /shift 09:00-18:00 1-5 (1 = понедельник).")
            return
        try:
            hours, days = parse_shift(arg)
        except ConfigError as exc:
            await self.reply(f"Не понял: {exc}")
            return
        cfg = self.relay.cfg
        cfg.work_start, cfg.work_end = _hours(hours)
        self.member.work_hours = hours
        if days:
            cfg.work_days = _days(days)
            self.member.work_days = days
        if self.team is not None:
            self.team.save_member(self.member)
        self.relay.note(None, f"смена теперь {shift_text(cfg)}")
        await self.reply(f"✅ Смена: {shift_text(cfg)}. Вне смены заявки сам не беру и пришлю кнопку «Взять сейчас».")

    async def _work(self, arg: str) -> None:
        assess = self.relay.assess
        if not assess.on:
            await self.reply("Сейчас бот сам переносит заявки в «В работе» (REACTION_MODE=classic). "
                             "/work нужен в режиме оценки: REACTION_MODE=assess в .env.")
            return
        number = re.sub(r"\D", "", arg)
        ids = [int(number)] if number else assess.waiting_cards()
        if len(ids) == 1:
            result = await assess.start_work(ids[0])
            if result not in {"В работе", "Сначала категория"}:  # об этих двух бот уже написал подробно
                await self.reply(f"№{ids[0]}: {result}")
            return
        text, rows = assess.pick_text()
        await self.relay.alert.send(text, rows)

    async def _checks(self, arg: str) -> None:
        stats = self.relay.lessons
        if "csv" in arg.split() or "таблиц" in arg:
            data = b"\xef\xbb\xbf" + stats.csv_text().encode("utf-8")
            path = stats.export_csv()
            caption = ("Проверки перед уроком — одна строка на проблему, без контактов и текста отчётов. "
                       "Google Таблицы: Файл → Импорт; разделитель «;».")
            if await self.relay.alert.send_file("lesson-checks.csv", data, caption):
                return
            await self.reply("Не смог отправить файл." + (f" Он лежит здесь: {path}" if path else ""))
            return
        from .reports import Reporter
        reporter = self.reporter or Reporter(self.relay, self.detector)
        words = arg.split()
        kind = "month" if "month" in words or "месяц" in arg else "week"
        previous = "last" in words or "прошл" in arg
        today = self.relay.clock().astimezone(self.relay.cfg.tz).date()
        period = reporter.period(kind, today, previous=previous)
        await self.reply(stats.text(period.since, period.until, period.title))

    async def _quick_callback(self, action: str, card_id: int, extra: str | None) -> str:
        from .kaiten import KaitenError

        quick = self.relay.quick
        try:
            if action == "fcat" and extra:
                return "Записал категорию" if await quick.set_category(card_id, int(extra)) else "Такой категории нет"
            if action == "fall":
                await quick.show_all(card_id)
                return ""
            if action == "fsol" and extra:
                return "Записал" if await quick.pick_solution(card_id, int(extra)) else "Вариант устарел"
            if action == "fnr":
                return "Закрыл" if await quick.close_noreply(card_id) else ""
            if action == "freopen" and extra:
                return await quick.reopen(card_id, int(extra))
            if action == "fill":
                after_rate = extra == "1"  # кнопку показали после «оцените работу» — заявку надо закрыть
                if await quick.ask(card_id, after_rate=after_rate) or after_rate:
                    return ""
                return "Поля уже заполнены"
        except KaitenError as exc:
            await self.reply(f"❗ Kaiten: {exc}")
            return "Ошибка Kaiten"
        return ""

    async def _team_callback(self, action: str, first: str, second: str | None) -> str:
        team = self.team
        if action == "unjoin":
            return "Убрал" if await team.remove(int(first)) else "Его уже нет в команде"
        if action == "join":
            who = self.relay.storage.get(f"join_name:{first}", "") or "этого человека"
            people = team.candidates()
            if not people:
                await self.reply(f"Не вижу на доске ответственных, кого ещё нет в команде. Узнай id {who} в Kaiten "
                                 f"и добавь его в {self.relay.cfg.team_file}: chat_id {first}, kaiten_id, name.")
                return ""
            rows = [[{"text": name[:40], "callback_data": f"joinas:{first}:{kid}"}] for kid, name in people[:12]]
            await self.relay.alert.send(f"Кто {who} в Kaiten? Выбери из ответственных на доске:", rows)
            return ""
        if second is None:
            return ""
        name = next((n for kid, n in team.candidates() if kid == int(second)), "")
        if not name:
            return "Этого специалиста уже нет в списке"
        ok, text = await team.add(first, int(second), name)
        await self.reply(text)
        return "Готово" if ok else "Не добавил"

    def _similar(self, text: str) -> str:
        if not text:
            return "Напиши после команды текст заявки, например: /similar не проходит оплата картой не РФ"
        if self.relay.cases.get() is None:
            return ("База похожих случаев пустая. Выгрузи из Kaiten сводный отчёт (xlsx) и перетащи файл "
                    "на import_cases.bat в папке сервиса.")
        found = self.relay.cases.search(text, k=5)
        if not found:
            return "Похожих решённых заявок не нашёл."
        from .similar import format_hint
        return format_hint(found, self.relay.cfg.card_url_prefix)

    async def _stranger(self, msg: dict) -> None:
        """Кто-то чужой написал боту /start: спросить тебя, сделать ли его получателем сводок.

        Сам чужой человек не получает ничего, пока ты не нажмёшь кнопку."""
        if self.detector is None and self.team is None:
            return
        chat = msg.get("chat") or {}
        text = str(msg.get("text") or "").strip()
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or not re.match(r"^/start(@\w+)?\b", text):
            return
        store = self.detector.storage if self.detector else self.relay.storage
        manager = self.detector.manager_chat() if self.detector else ""
        corp = {self.relay.storage.get("business_owner_chat", "") or "", self.relay.storage.get("business_owner_id", "") or ""}
        if str(chat_id) in corp - {""}:
            return  # это сам корпоративный аккаунт открыл бота (/start) — сюда приходят готовые тексты
        if str(chat_id) == manager or (self.team is not None and self.team.member_by_chat(str(chat_id))):
            return
        asked = store.get(f"mgr_asked:{chat_id}", "") or ""
        now = self.relay.clock()
        if asked and (asked == "1" and self.team is None or
                      asked != "1" and now - datetime.fromisoformat(asked) < timedelta(minutes=10)):
            return  # не спамить: тот же человек может напомнить о себе не чаще раза в 10 минут
        store.set(f"mgr_asked:{chat_id}", now.isoformat())
        who = msg.get("from") or {}
        name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or "без имени"
        user = f" @{who['username']}" if who.get("username") else ""
        where = f" в группе «{chat.get('title')}»" if chat.get("type") in {"group", "supergroup"} else ""
        rows = []
        if self.detector is not None:
            rows.append([{"text": "👔 Руководитель — получать сводки", "callback_data": f"mgr:{chat_id}"}])
        if self.team is not None and chat.get("type") == "private":
            store.set(f"join_name:{chat_id}", f"{name}{user}")
            rows.append([{"text": "👤 Специалист поддержки — бот работает на него", "callback_data": f"join:{chat_id}"}])
        await self.relay.alert.send(
            f"🤝 Боту написал(а) {name}{user}{where}. Кто это? Если не знаешь — просто не нажимай.", rows)

    async def _callback(self, query: dict) -> None:
        sender = str((query.get("from") or {}).get("id"))
        data = str(query.get("data") or "")
        answer = ""
        match = re.fullmatch(r"manual:(\d+):(\w+)", data)
        if sender == self.owner and match:
            cancelled = await self.relay.mark_manual(int(match.group(1)), match.group(2))
            answer = "Понял, больше не пишу" if cancelled else "Там уже нечего отменять"
        elif sender == self.owner and (m := re.fullmatch(r"who:([0-9a-f]+):(me|other)", data)):
            answer = await self.relay.resolve_who(m.group(1), m.group(2))
        elif sender == self.owner and (m := re.fullmatch(r"take:(\d+)", data)):
            answer = await self.relay.take_now(int(m.group(1)))
        elif sender == self.owner and (m := re.fullmatch(r"speed:(\w+)", data)):
            text = self.relay.set_speed(m.group(1))
            answer = "Готово" if m.group(1) in SPEED_KEYS else "Не понял"
            await self.reply(text)
        elif sender == self.owner and data == "restart:yes":
            answer = "Перезапускаюсь"
            await self.reply("🔄 Перезапускаюсь. Через несколько секунд напишу, что снова работаю.")
            self.relay.storage.set("restart_requested", "1")
            self.relay.note(None, "перезапуск по команде /restart")
            if self.on_restart:
                self.on_restart()
        elif sender == self.owner and data == "stop:yes":
            answer = "Выключаюсь"
            await self.reply("⛔ Выключаюсь. Включить снова — запусти start.bat на компьютере.")
            self.relay.note(None, "выключен командой /stop")
            if self.on_stop:
                self.on_stop()
        elif sender == self.owner and self.admin and self.team is not None and \
                (m := re.fullmatch(r"(join|joinas|unjoin):(-?\d+)(?::(\d+))?", data)):
            answer = await self._team_callback(m.group(1), m.group(2), m.group(3))
        elif sender == self.owner and self.detector and (m := re.fullmatch(r"mgr:(-?\d+)", data)):
            self.detector.set_manager(m.group(1))
            await self.detector.to_manager(WELCOME_MANAGER)
            answer = "Готово: руководитель будет получать сводки"
            await self.reply("👔 Руководитель подключён. Отключить: /manager off")
        elif sender == self.owner and (m := re.fullmatch(r"(fcat|fall|fsol|fill|fnr|freopen):(\d+)(?::(\d+))?", data)):
            answer = await self._quick_callback(m.group(1), int(m.group(2)), m.group(3))
        elif sender == self.owner and (m := re.fullmatch(r"(go|info|eta):(\d+)", data)):
            assess, card_id = self.relay.assess, int(m.group(2))
            if m.group(1) == "go":
                answer = await assess.start_work(card_id)
            elif m.group(1) == "eta":
                answer = await assess.start_work(card_id, eta_only=True)
            else:
                answer = "Ждём информацию" if await assess.to_info_wait(card_id) else "Карточка уже не в «Новой»"
        elif sender == self.owner and (m := re.fullmatch(r"(work|back|wait|keep):(\d+)(?::(\d+))?", data)):
            action, card_id = m.group(1), int(m.group(2))
            if action == "work":
                answer = "Перенёс в «В работе»" if await self.relay.move_to_work(card_id, None) else "Уже не в ожидании"
            elif action == "back" and m.group(3):
                answer = "Вернул" if await self.relay.move_back(card_id, int(m.group(3))) else "Карточка уже не в «В работе»"
            elif action == "wait":
                answer = ("Перенёс в «Ждём ответа»" if await self.relay.move_to_waiting(card_id)
                          else "Карточка уже не в «В работе»")
            elif action == "keep":
                answer = "Ок, не спрашиваю до следующего сообщения"
        if self.api is not None:
            try:
                await self.api.call("answerCallbackQuery", callback_query_id=query.get("id"), text=answer or None)
            except TelegramApiError as exc:
                log.debug("answerCallbackQuery: %s", exc)

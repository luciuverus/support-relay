"""Основная логика: взять заявку в работу, поставить сообщения в очередь, следить за SLA."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .assess import WORK_ROLE, Assess, eta_phrase
from .config import Config
from .humanize import SPEED_KEYS, SPEEDS, MessageStore, Pace, greeting, next_send_time
from .kaiten import KaitenClient, KaitenError
from .logic import (client_contact, client_contact_from_text, compute_deadline, extract_card_id, find_author, is_responsible, moved_at,
                    normalize_contact, normalize_people, recontact_columns, waiting_columns, warn_time)
from .logic import is_urgent
from .quick import QuickReplies
from .lessonstats import LessonStats
from .similar import CaseStore, card_query, format_hint
from .storage import Storage
from .transports import AlertBot, Clock, SendCancelled, SendError, Transport, utcnow

log = logging.getLogger(__name__)

# Второму адресату пишем чуть позже — как человек, который отвечает по очереди.
STAGGER_S = (20.0, 70.0)
# Между «пузырями» одного сообщения (плюс время «печатает…» самого пузыря).
BUBBLE_GAP_S = 1.0
GREETING_ROLES = ("employee", "client")
FOLLOWUP_ROLE = "followup"
CORP_HINT = ("готовый текст я отправил в чат корпоративного аккаунта с ботом — нажми кнопку там, и чат с человеком "
             "откроется сразу от корпоративного аккаунта")


def load_employees(path: str) -> dict[str, str]:
    """employees.json: {"email автора или id в Kaiten": "@username / chat id"}."""
    file = Path(path)
    if not file.exists():
        log.warning("Файл %s не найден — авторам заявок писать не будем", path)
        return {}
    with open(file, encoding="utf-8") as fh:
        return normalize_people(json.load(fh))


class Relay:
    def __init__(self, cfg: Config, storage: Storage, kaiten: KaitenClient, transport: Transport,
                 alert: AlertBot, messages: MessageStore, employees: dict[str, str],
                 rng: random.Random | None = None, clock: Clock = utcnow):
        self.cfg = cfg
        self.storage = storage
        self.kaiten = kaiten
        self.transport = transport
        if getattr(transport, "pace", None) is None:
            transport.pace = self.typing_pace  # «печатает…» идёт в темпе /speed
        self.alert = alert
        self.messages = messages
        self.employees = employees
        self.rng = rng or random.Random()
        self.clock = clock
        self._locks: dict[int, asyncio.Lock] = {}
        self._failure_alerted: set[int] = set()  # не спамить одной и той же ошибкой при опросе
        self._field_keys: dict[str, str] = {}      # «Ваш телеграм» -> "id_123"
        self._field_alerted: set[str] = set()
        self._props_cache: list[dict] | None = None
        self._props_loaded_at: datetime | None = None
        self.name = cfg.specialist_name or "специалист"
        self.started_at = clock()
        self._waiting_ids: list[int] | None = sorted(cfg.waiting_column_ids) or None
        self._recontact_ids: list[int] | None = sorted(cfg.recontact_column_ids) or None
        self._waiting_lookup_at: datetime | None = None
        self._followup_next_check: dict[int, datetime] = {}
        self._boards: dict[int, tuple[datetime, dict]] = {}
        self.cases = CaseStore(cfg.cases_file)
        self.quick = QuickReplies(self)
        self.lessons = LessonStats(storage, kaiten, cfg, clock)  # статистика проверок перед уроком
        self.assess = Assess(self)  # REACTION_MODE=assess: в работу — только после оценки
        self.is_primary = True  # главный специалист (из .env); у коллег из team.json — False
        self.tasks: list = []   # фоновые циклы этого специалиста (чтобы остановить, если его убрали)

    def set_kaiten(self, client: KaitenClient) -> None:
        """Свой API-ключ Kaiten у коллеги: переносы в истории карточек — от его имени."""
        self.kaiten = client
        self.quick.kaiten = client
        self.assess.kaiten = client
        self.lessons.kaiten = client

    async def resolve_name(self) -> str:
        """Имя для {name}: SPECIALIST_NAME, иначе первое слово имени из профиля Kaiten."""
        if self.cfg.specialist_name:
            return self.name
        try:
            me = await self.kaiten.current_user()
            full = str((me or {}).get("full_name") or (me or {}).get("username") or "").strip()
            if full:
                self.name = full.split()[0]
        except KaitenError as exc:
            log.warning("Не смог узнать имя из профиля Kaiten: %s", exc)
        return self.name

    def _fmt(self, dt: datetime, with_date: bool = True) -> str:
        local = dt.astimezone(self.cfg.tz)
        return local.strftime("%d.%m %H:%M" if with_date else "%H:%M")

    def _deadline_phrase(self, deadline: datetime) -> str:
        now_local = self.clock().astimezone(self.cfg.tz)
        local = deadline.astimezone(self.cfg.tz)
        if local.date() == now_local.date():
            return f"сегодня до {local:%H:%M}"
        if local.date() == now_local.date() + timedelta(days=1):
            return f"завтра до {local:%H:%M}"
        return f"до {local:%d.%m %H:%M}"

    def _values(self, card_id: int, title: str, deadline: datetime, at: datetime) -> dict:
        return {
            "title": title,
            "card_id": card_id,
            "deadline": self._deadline_phrase(deadline),
            "deadline_time": self._fmt(deadline, with_date=False),
            "deadline_full": self._fmt(deadline),
            "greeting": greeting(at.astimezone(self.cfg.tz)),
            "name": self.name,
            "eta": eta_phrase(self.cfg.assess_eta_default_min),
        }

    def _in_work_hours(self, at: datetime) -> bool:
        local = at.astimezone(self.cfg.tz)
        return (local.isoweekday() in self.cfg.work_days
                and self.cfg.work_start <= local.time() < self.cfg.work_end)

    # --- темп сообщений (/speed) ---
    def pace(self) -> Pace:
        """Текущий темп: выбранный командой /speed, иначе SPEED из .env."""
        key = self.storage.get("speed", "") or self.cfg.speed
        if key in SPEEDS:
            return SPEEDS[key]
        return Pace("custom", "⚙️ как в .env", self.cfg.delay_min_s, self.cfg.delay_max_s,
                    self.cfg.typing_cps_min, self.cfg.typing_cps_max, 45.0, STAGGER_S)

    def typing_pace(self) -> tuple[float, float, float]:
        pace = self.pace()
        return pace.cps_min, pace.cps_max, pace.typing_max_s

    def set_speed(self, key: str) -> str:
        if key not in SPEED_KEYS:
            return "Не понял. Варианты: /speed instant, /speed fast, /speed slow, /speed custom."
        self.storage.set("speed", key)
        pace = self.pace()
        self.note(None, f"темп сообщений: {pace.label}")
        return f"Темп сообщений — {pace.describe()}.\nНовые сообщения пойдут уже так."

    def _send_time(self) -> datetime:
        """Для напоминаний «актуально ли?»: с человеческой паузой и только в рабочее время."""
        pace = self.pace()
        return next_send_time(self.clock(), self.cfg.tz, self.cfg.work_start, self.cfg.work_end,
                              self.cfg.work_days, self.rng, pace.delay_min_s, pace.delay_max_s)

    def _greeting_time(self) -> datetime:
        """Для приветствия по взятой заявке: только человеческая пауза, рабочее время НЕ ждём.

        Регламент 4.2: при переводе «В работу» связаться с клиентом обязательно — заявку на тебя
        назначили, значит, ты на смене, и откладывать сообщение до утра нельзя.
        """
        pace = self.pace()
        return self.clock() + timedelta(seconds=self.rng.uniform(pace.delay_min_s, pace.delay_max_s))

    # --- пауза (/pause, /resume) ---
    async def is_paused(self) -> bool:
        value = self.storage.get("paused_until", "") or ""
        if not value:
            return False
        if value == "forever":
            return True
        if self.clock() >= datetime.fromisoformat(value):
            self.storage.set("paused_until", "")
            await self.alert.send("▶️ Пауза закончилась — снова беру заявки.")
            return False
        return True

    def pause(self, hours: float | None) -> str:
        if hours:
            until = self.clock() + timedelta(hours=hours)
            self.storage.set("paused_until", until.isoformat())
            return f"⏸ Пауза до {self._fmt(until)}. Заявки не беру, никому не пишу. /resume — снять раньше."
        self.storage.set("paused_until", "forever")
        return "⏸ Пауза, пока не напишешь /resume. Заявки не беру, никому не пишу."

    def resume(self) -> str:
        self.storage.set("paused_until", "")
        return "▶️ Снова работаю: беру заявки и пишу людям."

    PREFILL_MAX = 600  # длиннее — Telegram может не принять кнопку; тогда текст всё равно есть в сообщении

    def _buttons(self, card_id: int, contact_raw: str | None = None, role: str | None = None,
                 prefill: str | None = None) -> list[list[dict]]:
        row = [{"text": "Открыть карточку", "url": f"{self.cfg.card_url_prefix}{card_id}"}]
        contact = normalize_contact(contact_raw or "")
        rows = [row]
        if contact and contact.kind == "username":
            if prefill:
                # чат откроется с уже вписанным текстом — останется нажать «Отправить»
                text = quote(prefill[:self.PREFILL_MAX], safe="")
                rows.insert(0, [{"text": "✍️ Открыть чат с готовым текстом",
                                 "url": f"https://t.me/{contact.value}?text={text}"}])
            else:
                row.append({"text": "Открыть чат", "url": f"https://t.me/{contact.value}"})
        if role:
            rows.append([{"text": "✅ Отправил вручную", "callback_data": f"manual:{card_id}:{role}"}])
        return rows

    async def field_key(self, spec: str) -> str | None:
        """Ключ поля карточки ("id_123") по его id или названию, как в интерфейсе Kaiten."""
        spec = (spec or "").strip()
        if not spec:
            return None
        if spec in self._field_keys:
            return self._field_keys[spec]
        direct = re.fullmatch(r"(?:id_)?(\d+)", spec)
        if direct:
            self._field_keys[spec] = f"id_{direct.group(1)}"
            return self._field_keys[spec]
        now = self.clock()
        stale = self._props_loaded_at is None or now - self._props_loaded_at > timedelta(minutes=10)
        if self._props_cache is None or stale:
            try:
                self._props_cache = await self.kaiten.list_custom_properties()
                self._props_loaded_at = now
            except KaitenError as exc:
                log.warning("Не смог получить список полей Kaiten: %s", exc)
                return None
        wanted = spec.casefold()
        props = [p for p in self._props_cache or [] if isinstance(p.get("name"), str) and p.get("id") is not None]
        exact = [p for p in props if p["name"].strip().casefold() == wanted]
        partial = [p for p in props if wanted in p["name"].casefold()]
        match = exact[0] if exact else (partial[0] if len(partial) == 1 else None)
        if match is None:
            if spec not in self._field_alerted and self.is_primary:
                self._field_alerted.add(spec)
                similar = [p["name"] for p in props if "телеграм" in p["name"].casefold()
                           or "telegram" in p["name"].casefold()]
                hint = f" Похожие: {', '.join(similar)}." if similar else ""
                many = f" Подходит несколько: {', '.join(p['name'] for p in partial)}." if len(partial) > 1 else ""
                await self.alert.send(f"⚠️ Не нашёл в Kaiten поле «{spec}».{many}{hint} "
                                      "Проверь название в .env (AUTHOR_TG_FIELD / CLIENT_TG_FIELD).")
            return None
        self._field_keys[spec] = f"id_{match['id']}"
        return self._field_keys[spec]

    async def texts(self):
        """Актуальные тексты; если в messages.json ошибка — один раз сообщаем тебе."""
        book = self.messages.book
        error = self.messages.pop_error() if self.is_primary else None  # ошибку в messages.json — главному
        if error:
            await self.alert.send(f"⚠️ В messages.json ошибка, продолжаю со старыми текстами: {error}")
        return book

    # --- опрос Kaiten (без вебхука и без прав администратора) ---
    async def poll_queue(self) -> list[str]:
        """Смотрит «Очередь» и берёт карточки, где ты ответственный. Возвращает результаты по ним."""
        results: list[str] = []
        paused = await self.is_paused()
        for column_id in sorted(self.cfg.queue_column_ids):
            try:
                cards = await self.kaiten.list_cards(column_id)
            except KaitenError as exc:
                log.warning("Опрос колонки %s не удался: %s", column_id, exc)
                continue
            for item in cards:
                card_id = item.get("id")
                if not isinstance(card_id, int):
                    continue
                # В списке обычно уже есть участники; если их нет — проверим по полной карточке.
                has_people = "members" in item or "responsible_id" in item
                if has_people and not is_responsible(item, self.cfg.my_user_id):
                    continue
                if self.assess.on and self.assess.state(card_id) and self.storage.has_card(card_id):
                    results.append("ignored:assessing")  # поздоровались, ждём твоей оценки
                    continue
                if paused:
                    results.append(await self._alert_while_paused(item))
                    continue
                if self._off_shift():
                    results.append(await self._alert_off_shift(item))
                    continue
                lock = self._locks.setdefault(card_id, asyncio.Lock())
                async with lock:
                    results.append(await self._process_card(card_id))
        return results

    # --- вебхук ---
    async def handle_webhook(self, payload: dict) -> str:
        card_id = extract_card_id(payload)
        if card_id is None:
            return "ignored:no-card"
        if self._off_shift():
            return await self._alert_off_shift({"id": card_id})
        lock = self._locks.setdefault(card_id, asyncio.Lock())
        async with lock:
            return await self._process_card(card_id)

    def _off_shift(self) -> bool:
        """До начала или после конца смены заявки сами не берём (TAKE_OUTSIDE_HOURS=false)."""
        return not self.cfg.take_outside_hours and not self._in_work_hours(self.clock())

    EARLY_WINDOW = timedelta(hours=3)

    def _someone_else_on_duty(self, at: datetime) -> bool:
        """Вне смены сообщения с общего аккаунта пишет дежурный, а не ты.

        Исключение: ты сам нажал «Взять сейчас» вне смены — следующие 3 часа считаем, что работаешь ты."""
        if self.cfg.take_outside_hours or self._in_work_hours(at):
            return False
        early = self.storage.get("early_until", "") or ""
        return not (early and at < datetime.fromisoformat(early))

    async def take_now(self, card_id: int) -> str:
        """Кнопка «Взять сейчас» на уведомлении вне смены."""
        lock = self._locks.setdefault(card_id, asyncio.Lock())
        async with lock:
            result = await self._process_card(card_id)
        if result == "taken":
            self.storage.set("early_until", (self.clock() + self.EARLY_WINDOW).isoformat())
        return {"taken": "Взял в работу", "ignored:already-in-work": "Уже в работе",
                "ignored:not-mine": "Заявка уже не на тебе",
                "ignored:not-in-queue": "Заявка уже не в очереди"}.get(result, "Не получилось — открой карточку")

    async def _alert_off_shift(self, item: dict) -> str:
        """Вне смены: одно уведомление с кнопкой «Взять сейчас»; без нажатия — возьмём в начале смены."""
        card_id = item["id"]
        if self.storage.get(f"offshift_alerted:{card_id}"):
            return "offshift:already-alerted"
        try:
            card = await self.kaiten.get_card(card_id)
        except KaitenError:
            return "offshift:error"
        if not card or not is_responsible(card, self.cfg.my_user_id) \
                or card.get("column_id") not in self.cfg.queue_column_ids:
            return "offshift:not-mine"
        self.storage.set(f"offshift_alerted:{card_id}", self.clock().isoformat())
        title = card.get("title") or card_id
        start = f"{self.cfg.work_start:%H:%M}"
        urgent = "🔥 СРОЧНАЯ. " if is_urgent(card) else ""
        self.note(card_id, f"назначили вне смены — не взял, жду начала смены ({start})")
        await self.alert.send(
            f"🌙 {urgent}Смена ещё не началась (или уже закончилась), а на тебя назначили «{title}». "
            f"Сам не беру и никому не пишу — возьму в начале смены, в {start}. "
            "Если ты уже на месте — нажми «Взять сейчас».",
            [[{"text": "▶️ Взять сейчас", "callback_data": f"take:{card_id}"}], *self._buttons(card_id)])
        return "offshift:alerted"

    async def _process_card(self, card_id: int) -> str:
        taken_before = self.storage.has_card(card_id)
        if taken_before and self.cfg.dry_run:
            return "ignored:already-taken"  # в тесте колонка не меняется — не повторяемся
        try:
            card = await self.kaiten.get_card(card_id)
        except KaitenError as exc:
            log.warning("Не смог получить карточку %s: %s", card_id, exc)
            return "error:kaiten"
        if not card:
            return "ignored:card-not-found"
        if not is_responsible(card, self.cfg.my_user_id):
            return "ignored:not-mine"
        if card.get("column_id") == self.cfg.in_work_column_id:
            return "ignored:already-in-work"
        if card.get("column_id") not in self.cfg.queue_column_ids:
            # «Ждём ответа», «Проблемные» и т.п.: карточка уже в процессе — не трогаем
            return "ignored:not-in-queue"
        assess = self.assess.on
        if taken_before and assess and self.assess.state(card_id):
            return "ignored:assessing"
        if taken_before:
            # Уже брали, но карточка снова на тебе и не в «В работе» — её переоткрыли.
            self.storage.release_card(card_id)
        now = self.clock()
        deadline = compute_deadline(card, now, self.cfg.sla_minutes)
        title = str(card.get("title") or f"Заявка {card_id}")
        url = f"{self.cfg.card_url_prefix}{card_id}"
        if not self.storage.claim_card(card_id, title, url, now, deadline,
                                       warn_time(now, deadline, self.cfg.sla_warn_share)):
            return "ignored:already-taken"
        book = await self.texts()
        last = self.storage.get("last_variant:kaiten_comment")
        rendered = book.render("kaiten_comment", self._values(card_id, title, deadline, now),
                               int(last) if last else None)
        comment = None
        if rendered:
            bubbles, variant = rendered
            comment = "\n".join(bubbles)
            self.storage.set("last_variant:kaiten_comment", str(variant))
        try:
            if self.cfg.dry_run:
                log.info("[dry-run] взял бы в работу карточку %s, комментарий: %s", card_id, comment)
            elif not assess:
                await self.kaiten.move_card(card_id, self.cfg.in_work_column_id, self.cfg.in_work_board_id)
        except KaitenError as exc:
            self.storage.release_card(card_id)
            if card_id not in self._failure_alerted:
                self.note(card_id, f"не смог взять в работу: {exc}")
                self._failure_alerted.add(card_id)
                await self.alert.send(f"❗ Не смог взять в работу «{title}»: {exc}\n"
                                      f"Буду пробовать ещё, но лучше возьми вручную.", self._buttons(card_id))
            return "error:kaiten"
        self._failure_alerted.discard(card_id)
        comment_warning = None
        if assess:
            self.assess.begin(card, now)
        if comment and not self.cfg.dry_run:
            # Карточка уже «В работе» — сбой комментария не должен отменять взятие и приветствие.
            try:
                await self.kaiten.add_comment(card_id, comment)
            except KaitenError as exc:
                comment_warning = f"не записал комментарий в карточку ({exc}) — напиши его сам"

        warnings, previews, author_raw = await self._queue_messages(card, card_id, title, deadline, book)
        if comment_warning:
            warnings.insert(0, comment_warning)
        text = f"🆕 L2: «{title}»\nSLA: {self._fmt(deadline)}\n{url}"
        if assess:
            text = (f"🆕 L2: «{title}»\n{url}\nПоздороваюсь, а карточка останется в «Новой» — время реакции идёт, "
                    "пока ты не возьмёшь её в работу. Хватает информации и заполнена фактическая категория — "
                    f"«▶️ В работу» (или /work {card_id}); нужно уточнить у клиента — «⏸ Ждём инфо».")
        if is_urgent(card):
            text = ("🔥 СРОЧНАЯ ЗАЯВКА — по регламенту (п. 4.4) в приоритете. Если ведёшь другую, "
                    "напиши тому клиенту, что вернёшься позже.\n" + text)
        if warnings:
            text += "\n⚠️ " + "\n⚠️ ".join(warnings)
        hint = self.similar_hint(card, card_id)
        if hint:
            text += "\n\n" + hint
        if self.cfg.dry_run:
            lines = [f"Kaiten: {comment}" if comment else "Kaiten: комментарий выключен"]
            lines += [f"{role}: {msg}" for role, msg in previews]
            text = ("[тест, в Kaiten и людям ничего не отправлено]\n" + text
                    + "\n\nЧто было бы написано:\n" + "\n".join(lines))
        rows = self._buttons(card_id, author_raw)
        if assess:
            rows.append([{"text": "▶️ В работу", "callback_data": f"go:{card_id}"},
                         {"text": "⏸ Ждём инфо", "callback_data": f"info:{card_id}"}])
        await self.alert.send(text, rows)
        if assess:
            self.note(card_id, ("[тест] " if self.cfg.dry_run else "") + "поздоровался, жду оценку — карточка в «Новой»"
                      + (" (срочная)" if is_urgent(card) else "") + (" — " + "; ".join(warnings) if warnings else ""))
            return "greeted"
        self.note(card_id, ("[тест] взял бы в работу" if self.cfg.dry_run else "взял в работу")
                  + (" (срочная)" if is_urgent(card) else "") + (" — " + "; ".join(warnings) if warnings else ""))
        return "taken"

    def similar_hint(self, card: dict, card_id: int | None = None) -> str:
        """«Похожие случаи — тогда помогло: …» для уведомления о взятой заявке."""
        try:
            found = self.cases.search(card_query(card), k=self.cfg.similar_hints, exclude_id=card_id)
        except Exception as exc:  # подсказка — не главное, взятие заявки не должно из-за неё падать
            log.warning("Похожие случаи: %s", exc)
            return ""
        return format_hint(found, self.cfg.card_url_prefix)

    async def _alert_while_paused(self, item: dict) -> str:
        """На паузе заявки не берём, но о назначенной на тебя — предупреждаем (регламент 4.1: 5 минут)."""
        card_id = item["id"]
        if self.storage.get(f"pause_alerted:{card_id}"):
            return "paused:already-alerted"
        try:
            card = await self.kaiten.get_card(card_id)
        except KaitenError:
            return "paused:error"
        if not card or not is_responsible(card, self.cfg.my_user_id) \
                or card.get("column_id") not in self.cfg.queue_column_ids:
            return "paused:not-mine"
        self.storage.set(f"pause_alerted:{card_id}", self.clock().isoformat())
        urgent = "🔥 СРОЧНАЯ. " if is_urgent(card) else ""
        await self.alert.send(f"⏸ {urgent}Ты на паузе, но на тебя назначили заявку «{card.get('title') or card_id}». "
                              "По регламенту её нужно взять за 5 минут — возьми вручную или напиши /resume.",
                              self._buttons(card_id))
        return "paused:alerted"

    async def author_contact(self, card: dict) -> tuple[str, str | None]:
        """Telegram автора: поле «Ваш телеграм», иначе employees.json. (контакт, предупреждение)."""
        author_key = await self.field_key(self.cfg.author_tg_field)
        raw = client_contact(card, author_key) if author_key else ""
        emails: list[str] = []
        if not raw:
            _, raw, emails = find_author(card, self.employees, self.cfg.my_user_id)
        if raw:
            return raw, None
        who = f" ({', '.join(emails[:3])})" if emails else ""
        return "", (f"у автора{who} пустое поле «{self.cfg.author_tg_field}» и его нет "
                    "в employees.json — напиши вручную")

    async def _queue_messages(self, card: dict, card_id: int, title: str, deadline: datetime,
                              book) -> tuple[list[str], list[tuple[str, str]], str]:
        warnings: list[str] = []
        previews: list[tuple[str, str]] = []
        send_at = self._greeting_time()
        stagger = self.pace().stagger
        values = self._values(card_id, title, deadline, send_at)
        targets: list[tuple[str, str, str]] = []  # (role, raw contact, template kind)
        author_raw = ""
        if book.enabled("employee_taken"):
            # 1) поле «Ваш телеграм» на карточке; 2) запасной вариант — config/employees.json
            author_raw, problem = await self.author_contact(card)
            if author_raw:
                targets.append(("employee", author_raw, "employee_taken"))
            else:
                warnings.append(problem)
        client_raw = ""
        if await self._client_lane(card):
            client_key = await self.field_key(self.cfg.client_tg_field)
            written = client_contact(card, client_key) if client_key else ""
            client_raw = client_contact_from_text(written)
            if written and not client_raw:
                warnings.append(f"контакт клиента «{written[:60]}» — не Telegram, пишу только автору заявки")
        if client_raw and book.enabled("client_taken"):
            # 2-й уровень: проблема у родителя/ученика — здороваемся с ним, а куратору коротко «взял, пишу клиенту»
            if self.cfg.client_lanes:
                targets.insert(0, ("client", client_raw, "client_taken"))  # сначала тот, у кого проблема
                if book.enabled("curator_note"):
                    targets = [(r, raw, "curator_note" if r == "employee" else k) for r, raw, k in targets]
            else:
                targets.append(("client", client_raw, "client_taken"))

        seen: set[str] = set()
        for index, (role, raw, kind) in enumerate(targets):
            contact = normalize_contact(raw)
            if contact is None:
                warnings.append(f"не понял контакт {role}: {raw!r}")
                continue
            if contact.key in seen:
                continue
            seen.add(contact.key)
            greeted = self.storage.get(f"greeted:{card_id}:{contact.key}")
            if greeted:
                ago = self.clock() - datetime.fromisoformat(greeted)
                if ago < timedelta(hours=self.cfg.regreet_after_h):
                    # карточку вернули в «Очередь» вскоре после взятия — второй раз не здороваемся
                    warnings.append(f"{raw}: уже писал по этой заявке {int(ago.total_seconds() // 60)} мин "
                                    "назад — повторно не здороваюсь")
                    continue
            self.storage.set(f"greeted:{card_id}:{contact.key}", self.clock().isoformat())
            last = self.storage.get(f"last_variant:{kind}:{contact.key}")
            bubbles, variant = book.render(kind, values, int(last) if last else None)
            self.storage.set(f"last_variant:{kind}:{contact.key}", str(variant))
            when = send_at + timedelta(seconds=index * self.rng.uniform(*stagger))
            series = self._new_series(card_id, role)
            for position, text in enumerate(bubbles):
                # «пузыри» одного сообщения идут строго по порядку с короткой паузой
                self.storage.enqueue(card_id, role, raw, text,
                                     when + timedelta(seconds=position * BUBBLE_GAP_S), self.clock(), series)
            previews.append((role, " ⏎ ".join(bubbles)))
        return warnings, previews, author_raw

    async def _client_lane(self, card: dict) -> bool:
        """Писать ли клиенту на этом уровне (CLIENT_LANES). Пусто в настройке — на всех уровнях."""
        if not self.cfg.client_tg_field:
            return False
        if not self.cfg.client_lanes:
            return True
        title = (await self.lane_title(card)).casefold()
        return bool(title) and any(lane in title for lane in self.cfg.client_lanes)

    async def lane_title(self, card: dict) -> str:
        """Название дорожки (уровня) карточки: «2-ой уровень» и т.п. Пусто, если не узнать."""
        board_id, lane_id = card.get("board_id"), card.get("lane_id")
        lane = card.get("lane")
        if isinstance(lane, dict) and lane.get("title"):
            return str(lane["title"]).strip()
        if not board_id or lane_id is None:
            return ""
        cached = self._boards.get(board_id)
        if not cached or self.clock() - cached[0] > timedelta(hours=1):
            try:
                cached = (self.clock(), await self.kaiten.get_board(board_id) or {})
            except KaitenError as exc:
                log.warning("Не смог прочитать дорожки доски %s: %s", board_id, exc)
                return ""
            self._boards[board_id] = cached
        for item in cached[1].get("lanes") or []:
            if isinstance(item, dict) and item.get("id") == lane_id:
                return str(item.get("title") or item.get("name") or "").strip()
        return ""

    # --- очередь отправки ---
    async def _still_relevant(self, card_id: int, role: str, cache: dict) -> bool | None:
        """Перед первым сообщением группы: заявка всё ещё твоя и там, где надо? None — Kaiten недоступен."""
        if card_id not in cache:
            try:
                cache[card_id] = await self.kaiten.get_card(card_id)
            except KaitenError as exc:
                log.warning("Не смог проверить карточку %s перед отправкой: %s", card_id, exc)
                return None
        card = cache[card_id]
        if not card or not is_responsible(card, self.cfg.my_user_id):
            return False
        if role == FOLLOWUP_ROLE:
            return card.get("column_id") in set(self._waiting_ids or []) | set(self._recontact_ids or [])
        if role != WORK_ROLE and self.assess.on and self.assess.state(card_id):
            # поздоровались, а в работу ещё не брали: карточка в «Новой» или ждёт информации для оценки
            allowed = set(self.cfg.queue_column_ids) | set(await self.assess.wait_columns()) | {self.cfg.in_work_column_id}
            return card.get("column_id") in allowed
        return card.get("column_id") == self.cfg.in_work_column_id

    async def send_due(self) -> int:
        """Отправляет всё, что пора. Возвращает число успешно отправленных сообщений.

        Сообщения одному человеку по одной заявке — это группа «пузырей»: они уходят строго
        по порядку; если один не ушёл, остальные не отправляются вразнобой.
        """
        await self.texts()  # подхватить правки messages.json и сразу сообщить об ошибке в нём
        if await self.is_paused():
            return 0
        await self._expire_held()
        sent = 0
        stopped: set[tuple[int, str]] = set()
        checked: set[tuple[int, str]] = set()
        cards: dict = {}
        for item in self.storage.due_messages(self.clock()):
            group = (item["card_id"], item["role"])
            if group in stopped:
                continue
            contact = normalize_contact(item["contact"])
            if contact is None:
                self.storage.fail_group(*group, "bad contact")
                stopped.add(group)
                continue
            if self.cfg.dry_run:
                log.info("[dry-run] -> %s: %s", item["contact"], item["text"])
                if not self.storage.group_started(*group, item.get("series") or ""):
                    self.note(item["card_id"], f"[тест] написал бы {self.WHO.get(item['role'], item['role'])}")
                self.storage.set_message_status(item["id"], "sent", at=self.clock())
                sent += 1
                continue
            if self.storage.message_status(item["id"]) != "pending":
                stopped.add(group)  # отменили, пока отправлялись предыдущие «пузыри»
                continue
            series = item.get("series") or ""
            if group not in checked and not self.storage.group_started(*group, series):
                checked.add(group)
                relevant = await self._still_relevant(item["card_id"], item["role"], cards)
                if relevant is None:
                    stopped.add(group)
                    self._reschedule_group(group, 60)
                    continue
                if not relevant:
                    stopped.add(group)
                    self.storage.fail_group(*group, "заявка уже не твоя или не в нужной колонке", "cancelled")
                    self.note(item["card_id"], f"не стал писать {self.WHO.get(item['role'], item['role'])}: "
                                               "заявку переназначили или перенесли")
                    log.info("Не пишу по №%s (%s): заявку переназначили или перенесли", *group)
                    continue
            first_of_series = not self.storage.group_started(*group, series)
            try:
                await self.transport.send(contact, item["text"],
                                          still_wanted=lambda i=item["id"]: self.storage.message_status(i) == "pending")
            except SendCancelled:
                stopped.add(group)
                self.note(item["card_id"], f"отменил сообщение {self.WHO.get(item['role'], item['role'])} "
                                           "прямо во время «печатает…»")
                log.info("Не отправил по №%s (%s): отменили, пока бот печатал", *group)
                continue
            except SendError as exc:
                stopped.add(group)
                if exc.retry_after_s and exc.retry_after_s < 3600:
                    self._reschedule_group(group, exc.retry_after_s)
                    continue
                if exc.reason == "unknown_chat" and self.cfg.hold_unknown_chat_h > 0 \
                        and not self.storage.group_started(*group, series):
                    self.storage.fail_group(*group, str(exc), "held")
                    self.note(item["card_id"], f"жду, пока {item['contact']} напишет на корпоративный аккаунт "
                                               "(бот не может написать первым)")
                    if item["role"] == FOLLOWUP_ROLE:
                        note = (f"Напомню сам, как только {item['contact']} напишет на корпоративный аккаунт "
                                f"(жду до {self.cfg.hold_unknown_chat_h:g} ч).")
                    held_text = "\n".join(r["text"] for r in self.storage.pending_group(*group, ("held",)))
                    in_corp = await self._corp_card(item["card_id"], item["role"], item["contact"], held_text)
                    if item["role"] == FOLLOWUP_ROLE:
                        note = (f"Напомню сам, как только {item['contact']} напишет на корпоративный аккаунт "
                                f"(жду до {self.cfg.hold_unknown_chat_h:g} ч).")
                    else:
                        where = (CORP_HINT if in_corp else
                                 "нажми «✍️ Открыть чат с готовым текстом» (сначала переключись на корпоративный "
                                 "аккаунт — иначе уйдёт с твоего личного) и отправь")
                        note = (f"❗ По регламенту (п. 4.2) напиши сейчас: {where} — больше ничего не нужно, твоё "
                                "сообщение я увижу и отмечу сам. Если не успеешь, отправлю сам, как только человек "
                                f"напишет на корпоративный аккаунт (жду до {self.cfg.hold_unknown_chat_h:g} ч).")
                    await self.alert.send(f"⏳ Заявка №{item['card_id']}: {exc}\n{note}\nТекст:\n" + held_text,
                                          self._buttons(item["card_id"], item["contact"], item["role"], held_text))
                    continue
                texts = self.storage.fail_group(*group, str(exc))
                self.note(item["card_id"], f"не смог написать {self.WHO.get(item['role'], item['role'])}: {exc}")
                in_corp = await self._corp_card(item["card_id"], item["role"], item["contact"], "\n".join(texts))
                await self.alert.send(f"✉️ Не отправил сообщение {self.WHO.get(item['role'], item['role'])} "
                                      f"{item['contact']} по заявке №{item['card_id']}: {exc}\n"
                                      + (CORP_HINT[0].upper() + CORP_HINT[1:] + ".\n" if in_corp else "")
                                      + "Напиши вручную:\n" + "\n".join(texts),
                                      self._buttons(item["card_id"], item["contact"], prefill="\n".join(texts)))
                continue
            # Telegram принял сообщение — значит, оно правда ушло (даже если отмена пришла в ту же секунду);
            # остальные «пузыри» серии после отмены уже не уйдут: их статус проверяется перед каждым.
            self.storage.set_message_status(item["id"], "sent", at=self.clock())
            if first_of_series:
                self.note(item["card_id"], f"написал {self.WHO.get(item['role'], item['role'])} ({item['contact']})")
            sent += 1
            await asyncio.sleep(0)  # отдаём управление между отправками
        return sent

    # --- готовый текст в чате корпоративного аккаунта с ботом ---
    async def _corp_card(self, card_id: int, role: str, contact_raw: str, text: str) -> bool:
        """Продублировать готовый текст в чат корпоративного аккаунта с ботом: оттуда кнопка откроет
        чат с клиентом уже в корпоративном аккаунте. True — получилось."""
        notify = getattr(self.transport, "notify_owner", None)
        contact = normalize_contact(contact_raw or "")
        if not self.cfg.corp_prefill or notify is None or self.cfg.dry_run or not contact \
                or contact.kind != "username" or not text:
            return False
        link = f"https://t.me/{contact.value}?text={quote(text[:self.PREFILL_MAX], safe='')}"
        who = self.WHO.get(role, role)
        message_id = await notify(
            f"✍️ Заявка №{card_id} — {self.name}: напиши {who} @{contact.value}. Бот не может написать первым.\n"
            f"Нажми кнопку — чат откроется здесь, в корпоративном аккаунте, с готовым текстом:\n\n{text}",
            [[{"text": "✍️ Открыть чат с готовым текстом", "url": link}]])
        if not message_id:
            if not self.storage.get("corp_chat_warned"):
                self.storage.set("corp_chat_warned", "1")
                await self.alert.send("ℹ️ Не могу писать готовые тексты в чат корпоративного аккаунта с ботом. "
                                      "Зайди в корпоративный аккаунт, открой этого бота и нажми /start — тогда "
                                      "кнопка «с готовым текстом» будет открывать чат сразу от корпоративного аккаунта.")
            return False
        self.storage.set(f"corpmsg:{card_id}:{role}", f"{message_id}|{contact.key}")
        return True

    async def _drop_corp_card(self, card_id: int, role: str) -> None:
        """Сообщение уже ушло (сам бот или человек вручную) — убрать готовый текст из чата аккаунта."""
        await self._drop_corp_key(f"corpmsg:{card_id}:{role}")

    async def _drop_corp_key(self, key: str) -> None:
        message_id = (self.storage.get(key, "") or "").partition("|")[0]
        if not message_id.isdigit():
            return
        self.storage.set(key, "")
        drop = getattr(self.transport, "delete_owner_message", None)
        if drop is not None:
            await drop(int(message_id))

    async def _drop_corp_cards_for(self, keyset: set[str]) -> None:
        """Написали человеку вручную — его готовые тексты в чате аккаунта больше не нужны."""
        for key, value in self.storage.with_prefix("corpmsg:").items():
            if value.partition("|")[2] in keyset:
                await self._drop_corp_key(key)

    # --- журнал действий (/log) ---
    WHO = {"employee": "автору", "client": "клиенту", FOLLOWUP_ROLE: "«актуально ли?»",
           WORK_ROLE: "время решения"}
    JOURNAL_DAYS = 90    # журнал, чаты, заявки и отметки по ним
    MESSAGES_DAYS = 60   # тексты отправленных сообщений (нужны для отчёта за прошлый месяц)

    def note(self, card_id: int | None, action: str) -> None:
        """Одна строка в журнал: что бот сделал с заявкой. Видно в /log и в data/relay.log."""
        try:
            self.storage.journal_add(self.clock(), card_id, action)
        except Exception as exc:  # журнал не должен ломать работу
            log.warning("Журнал: %s", exc)
        log.info("Журнал №%s: %s", card_id, action)

    def journal_text(self, card_id: int | None = None) -> str:
        rows = self.storage.journal(card_id, limit=200 if card_id else 20)
        if not rows:
            return f"По заявке №{card_id} записей нет." if card_id else "Журнал пока пустой."
        head = f"📜 История заявки №{card_id}:" if card_id else "📜 Последние действия бота:"
        lines = [head]
        for r in rows:
            local = r["at"].astimezone(self.cfg.tz)
            num = "" if card_id or r["card_id"] is None else f" №{r['card_id']}"
            lines.append(f"{local:%d.%m %H:%M}{num} — {r['action']}")
        return "\n".join(lines)

    def prune_journal(self) -> bool:
        """Раз в сутки: журнал, чаты и заявки старше 90 дней, тексты отправленных сообщений старше 60."""
        now = self.clock()
        last = self.storage.get("journal_pruned_at", "")
        if last and now - datetime.fromisoformat(last) < timedelta(days=1):
            return False
        self.storage.journal_prune(now - timedelta(days=self.JOURNAL_DAYS))
        self.storage.prune_old(now - timedelta(days=self.MESSAGES_DAYS), now - timedelta(days=self.JOURNAL_DAYS),
                               now - timedelta(days=1))
        self.storage.set("journal_pruned_at", now.isoformat())
        return True

    def _new_series(self, card_id: int, role: str) -> str:
        return f"{card_id}:{role}:{self.clock().isoformat()}:{self.rng.randrange(1 << 30)}"

    def _reschedule_group(self, group: tuple[int, str], delay_s: float) -> None:
        start = self.clock() + timedelta(seconds=delay_s + self.rng.uniform(5, 30))
        for position, row in enumerate(self.storage.pending_group(*group)):
            self.storage.reschedule(row["id"], start + timedelta(seconds=position * BUBBLE_GAP_S))

    async def _expire_held(self) -> None:
        limit = timedelta(hours=self.cfg.hold_unknown_chat_h)
        for group in self.storage.groups_with_status("held"):
            if group["created_at"] and self.clock() - group["created_at"] > limit:
                self.storage.fail_group(group["card_id"], group["role"], "так и не написал", "failed", ("held",))
                await self.alert.send(f"⌛ Заявка №{group['card_id']}: {group['contact']} так и не написал(а) "
                                      "на корпоративный аккаунт — если ещё не связался, напиши вручную.",
                                      self._buttons(group["card_id"], group["contact"]))

    # --- события из Telegram ---
    async def on_chat_activity(self, keys: list[str], incoming: bool, manual: bool, at: datetime,
                               text: str = "") -> None:
        """Кто-то написал на корпоративный аккаунт (incoming) или ты сам ответил (manual)."""
        keyset = set(keys)

        def matches(raw: str) -> bool:
            contact = normalize_contact(raw)
            return bool(contact and contact.key in keyset)

        if incoming and "lesson-check" in text and self.is_primary:
            # отчёт проверки перед уроком — только в статистику (коды, без текста)
            await self.lessons.on_chat_text(text, at, ",".join(sorted(keyset)),
                                            lambda: self._single_card_for(keyset))
        if incoming:
            # 1) ждали, пока человек напишет, — теперь можно отправить приветствие
            for group in self.storage.groups_with_status("held"):
                if matches(group["contact"]):
                    start = self._send_time() if group["role"] == FOLLOWUP_ROLE else self._greeting_time()
                    self.storage.release_group(group["card_id"], group["role"], start, BUBBLE_GAP_S)
                    await self._drop_corp_card(group["card_id"], group["role"])
            # 2) ждали информацию для оценки — человек ответил, возвращаем карточку в «Новую»
            await self.assess.on_client_message(keyset)
            # 3) человек ответил сам — напоминание «актуально ли?» уже не нужно
            for group in self.storage.groups_with_status("pending"):
                if group["role"] == FOLLOWUP_ROLE and matches(group["contact"]) \
                        and not self.storage.group_started(group["card_id"], group["role"]):
                    self.storage.fail_group(group["card_id"], group["role"], "собеседник ответил", "cancelled")
        if not manual:
            return
        if await self.is_paused():
            # на паузе ты не работаешь — с общего аккаунта пишут коллеги, твои заявки не трогаем
            log.info("Пауза: написали с аккаунта в чат %s — заявки не трогаю", sorted(keyset))
            return
        if self._someone_else_on_duty(at):
            # вне твоей смены с общего аккаунта пишет дежурный — твои заявки не двигаем и не закрываем
            log.info("Вне смены написали с аккаунта в чат %s — это не ты, заявки не трогаю", sorted(keyset))
            return
        # Корпоративный аккаунт общий: это мог написать коллега по своей заявке тому же человеку.
        who = await self.who_wrote(keyset, text)
        if who == "colleague":
            log.info("Сообщение с аккаунта в чат %s — похоже, коллега; твои заявки не трогаю", sorted(keyset))
            return
        if who == "unclear":
            await self._ask_who(keyset, text)
            return
        await self._manual_actions(keyset, text)

    async def _manual_actions(self, keyset: set[str], text: str) -> None:
        """Ты сам написал автору: быстрый ответ, перенос «В работу», отмена своих неотправленных сообщений."""
        def matches(raw: str) -> bool:
            contact = normalize_contact(raw)
            return bool(contact and contact.key in keyset)

        handled = False
        if text:
            # быстрый ответ: «ждём ответ» → в ожидание (и тогда не возвращаем в работу), «закрываем» → поля
            handled = await self.quick.on_manual(keyset, text)
        if self.cfg.auto_to_work and not handled:
            await self.auto_to_work(keyset)
        # ты написал этому человеку сам — наши неотправленные сообщения ему уже лишние
        await self._drop_corp_cards_for(keyset)
        for status in ("pending", "held"):
            for group in self.storage.groups_with_status(status):
                if matches(group["contact"]) and not self.storage.group_started(group["card_id"], group["role"]):
                    self.storage.fail_group(group["card_id"], group["role"], "написал вручную", "cancelled",
                                            (status,))
                    await self._drop_corp_card(group["card_id"], group["role"])
                    self.note(group["card_id"], f"ты написал {group['contact']} сам — своё сообщение отменил")
                    await self.alert.send(f"👌 Ты написал {group['contact']} сам — моё сообщение по "
                                          f"№{group['card_id']} отменил.")

    # --- общий корпоративный аккаунт: ты или коллега? ---
    WHO_TTL = timedelta(minutes=30)

    async def _open_cards_with(self, keyset: set[str]) -> tuple[list[dict], list[dict]]:
        """Открытые карточки этого автора: (твои, коллег)."""
        await self.waiting_column_ids()
        columns = [*sorted(self.cfg.queue_column_ids), self.cfg.in_work_column_id,
                   *(self._waiting_ids or []), *(self._recontact_ids or [])]
        if self.assess.on:
            columns += await self.assess.wait_columns()
        mine, others = [], []
        for column_id in dict.fromkeys(columns):
            for item in await self.kaiten.list_cards(column_id):
                if not isinstance(item.get("id"), int) or not (await self.card_keys(item)) & keyset:
                    continue
                if "members" not in item and "responsible_id" not in item:
                    item = await self.kaiten.get_card(item["id"]) or item
                (mine if is_responsible(item, self.cfg.my_user_id) else others).append(item)
        return mine, others

    async def _single_card_for(self, keyset: set[str]) -> int | None:
        """Номер открытой карточки этого человека, если она одна (иначе не угадываем)."""
        mine, others = await self._open_cards_with(keyset)
        ids = {c["id"] for c in [*mine, *others]}
        return ids.pop() if len(ids) == 1 else None

    def _remembered_who(self, keyset: set[str]) -> str | None:
        for key in keyset:
            raw = self.storage.get(f"who:{key}", "")
            if raw:
                who, _, when = raw.partition("@")
                if self.clock() - datetime.fromisoformat(when) < self.WHO_TTL:
                    return who
        return None

    def _remember_who(self, keyset: set[str], who: str) -> None:
        for key in keyset:
            self.storage.set(f"who:{key}", f"{who}@{self.clock().isoformat()}")

    async def who_wrote(self, keyset: set[str], text: str) -> str:
        """"me" — ты (или неважно), "colleague" — коллега по своей заявке, "unclear" — спросить тебя."""
        try:
            mine, others = await self._open_cards_with(keyset)
        except KaitenError as exc:
            log.warning("Не смог проверить, чьё это сообщение: %s — считаю, что твоё", exc)
            return "me"
        if not others:
            return "me"            # с этим человеком работаешь только ты — как раньше
        if not mine:
            return "colleague"     # твоих заявок с ним нет — трогать нечего
        numbers = {int(n) for n in re.findall(r"(?<!\d)\d{7,9}(?!\d)", text)}
        if numbers & {c["id"] for c in mine}:
            return "me"
        if numbers & {c["id"] for c in others}:
            return "colleague"
        low = text.casefold().replace("ё", "е")
        if self.name and len(self.name) >= 3 and self.name.casefold().replace("ё", "е") in low:
            return "me"
        for card in others:
            for member in card.get("members") or []:
                if isinstance(member, dict) and member.get("type") == 2:
                    for word in str(member.get("full_name") or "").split():
                        if len(word) >= 3 and word.casefold().replace("ё", "е") in low:
                            return "colleague"
        return self._remembered_who(keyset) or "unclear"

    async def _ask_who(self, keyset: set[str], text: str) -> None:
        token = f"{self.rng.randrange(1 << 30):x}"
        self.storage.set(f"whoq:{token}", json.dumps({"keys": sorted(keyset), "text": text,
                                                     "at": self.clock().isoformat()}, ensure_ascii=False))
        mine, others = await self._open_cards_with(keyset)
        contact = next((k.split(":", 1)[1] for k in sorted(keyset) if k.startswith("username:")), None)
        who = f"@{contact}" if contact else "этим человеком"

        def names(cards):
            out = []
            for c in cards[:3]:
                person = next((m.get("full_name") for m in c.get("members") or []
                               if isinstance(m, dict) and m.get("type") == 2), "") or ""
                out.append(f"№{c['id']}" + (f" ({person.split()[-1]})" if person else ""))
            return ", ".join(out)
        await self.alert.send(
            f"🤔 В чат с {who} написали с корпоративного аккаунта: «{text.strip()[:80]}».\n"
            f"У этого человека есть твоя заявка ({names(mine)}) и заявка коллеги ({names(others)}). "
            "Это ты написал? Пока не ответишь, твою заявку не трогаю.",
            [[{"text": "Да, это я", "callback_data": f"who:{token}:me"},
              {"text": "Нет, коллега", "callback_data": f"who:{token}:other"}]])

    async def resolve_who(self, token: str, choice: str) -> str:
        raw = self.storage.get(f"whoq:{token}", "")
        if not raw:
            return "Уже неактуально"
        self.storage.set(f"whoq:{token}", "")
        data = json.loads(raw)
        keyset = set(data["keys"])
        if choice == "me":
            self._remember_who(keyset, "me")
            await self._manual_actions(keyset, data["text"])
            return "Понял, это ты"
        self._remember_who(keyset, "colleague")
        return "Понял, твои заявки не трогаю"

    # --- регламент п. 4.5: работа только в статусе «В работе» ---
    async def _card_author_key(self, item: dict) -> str | None:
        """Ключ чата автора карточки (username:…/id:…); кешируется, чтобы не дёргать Kaiten каждый раз."""
        card_id = item["id"]
        cached = self.storage.get(f"author_key:{card_id}")
        if cached is not None:
            return cached or None
        card = item if "properties" in item else await self.kaiten.get_card(card_id)
        raw, _ = await self.author_contact(card or {})
        contact = normalize_contact(raw)
        key = contact.key if contact else ""
        self.storage.set(f"author_key:{card_id}", key)
        return key or None

    async def _card_client_key(self, item: dict) -> str | None:
        """Ключ чата клиента (родителя/ученика) из карточки — на уровнях, где мы пишем клиенту."""
        card_id = item["id"]
        cached = self.storage.get(f"client_key:{card_id}")
        if cached is not None:
            return cached or None
        card = item if "properties" in item else await self.kaiten.get_card(card_id)
        key = ""
        if card and await self._client_lane(card):
            field = await self.field_key(self.cfg.client_tg_field)
            contact = normalize_contact(client_contact_from_text(client_contact(card, field) if field else ""))
            key = contact.key if contact else ""
        self.storage.set(f"client_key:{card_id}", key)
        return key or None

    async def card_keys(self, item: dict) -> set[str]:
        """Чаты, переписка в которых относится к этой карточке: автор и (на 2-м уровне) клиент."""
        keys = {await self._card_author_key(item), await self._card_client_key(item)}
        return {k for k in keys if k}

    async def _my_cards(self, column_ids) -> list[dict]:
        cards = []
        for column_id in column_ids:
            for item in await self.kaiten.list_cards(column_id):
                if not isinstance(item.get("id"), int):
                    continue
                if ("members" in item or "responsible_id" in item) and not is_responsible(item, self.cfg.my_user_id):
                    continue
                cards.append(item)
        return cards

    async def auto_to_work(self, keyset: set[str]) -> list[int]:
        """Ты написал автору сам → его карточку из «Ждём ответа»/«Повторно связаться» переносим «В работу»."""
        await self.waiting_column_ids()
        sources = [*(self._waiting_ids or []), *(self._recontact_ids or [])]
        if not sources:
            return []
        now = self.clock()
        try:
            found = []
            for item in await self._my_cards(sources):
                snooze = self.storage.get(f"no_auto:{item['id']}")
                if snooze and now < datetime.fromisoformat(snooze):
                    continue  # только что нажал «Вернуть обратно»
                if (await self.card_keys(item)) & keyset:
                    found.append(item)
        except KaitenError as exc:
            log.warning("Автоперенос в работу: не смог прочитать карточки: %s", exc)
            return []
        if len(found) > 1:
            rows = [[{"text": f"В работу: {str(c.get('title') or c['id'])[:40]}", "callback_data": f"work:{c['id']}"}]
                    for c in found[:6]]
            await self.alert.send("✍️ Ты написал автору, у которого несколько заявок в ожидании. "
                                  "Какую перенести в «В работе»?", rows)
            return []
        moved = []
        for item in found:
            if await self.move_to_work(item["id"], item.get("column_id"), item.get("title")):
                moved.append(item["id"])
        return moved

    async def move_to_work(self, card_id: int, from_column: int | None, title=None) -> bool:
        try:
            card = await self.kaiten.get_card(card_id)
            sources = set(self._waiting_ids or []) | set(self._recontact_ids or [])
            if not card or not is_responsible(card, self.cfg.my_user_id) or card.get("column_id") not in sources:
                return False
            from_column = card.get("column_id")
            if not self.cfg.dry_run:
                await self.kaiten.move_card(card_id, self.cfg.in_work_column_id, self.cfg.in_work_board_id)
        except KaitenError as exc:
            await self.alert.send(f"❗ Не смог перенести №{card_id} в «В работе»: {exc}. Перенеси вручную.",
                                  self._buttons(card_id))
            return False
        test = "[тест, в Kaiten не перенёс] " if self.cfg.dry_run else ""
        self.note(card_id, "[тест] " * self.cfg.dry_run + "перенёс в «В работе» — ты написал автору")
        rows = self._buttons(card_id)
        rows.append([{"text": "↩️ Вернуть обратно", "callback_data": f"back:{card_id}:{from_column}"}])
        await self.alert.send(f"🔁 {test}Перенёс «{card.get('title') or title or card_id}» в «В работе» — "
                              "ты написал автору (регламент п. 4.5).", rows)
        return True

    async def move_back(self, card_id: int, column_id: int) -> bool:
        """Кнопка «Вернуть обратно»: вернуть и 10 минут не переносить эту карточку автоматически."""
        self.storage.set(f"no_auto:{card_id}", (self.clock() + timedelta(minutes=10)).isoformat())
        moved = await self._move_if(card_id, {self.cfg.in_work_column_id}, column_id)
        if moved:
            self.note(card_id, "вернул обратно из «В работе» по кнопке")
        return moved

    async def move_to_waiting(self, card_id: int) -> bool:
        await self.waiting_column_ids()
        if not self._waiting_ids:
            return False
        moved = await self._move_if(card_id, {self.cfg.in_work_column_id}, self._waiting_ids[0])
        if moved:
            self.note(card_id, "[тест] " * self.cfg.dry_run + "перенёс в «Ждём ответа»")
        return moved

    async def _move_if(self, card_id: int, allowed_from: set[int], to_column: int) -> bool:
        try:
            card = await self.kaiten.get_card(card_id)
            if not card or not is_responsible(card, self.cfg.my_user_id) or card.get("column_id") not in allowed_from:
                return False
            if not self.cfg.dry_run:
                await self.kaiten.move_card(card_id, to_column)
            return True
        except KaitenError as exc:
            log.warning("Не смог перенести №%s: %s", card_id, exc)
            return False

    async def poll_wait_hints(self) -> list[str]:
        """«В работе», последнее слово наше, автор молчит WAIT_HINT_MIN минут → спросить про «Ждём ответа»."""
        results: list[str] = []
        if not self.cfg.wait_hint_min or await self.is_paused():
            return results
        await self.waiting_column_ids()
        if not self._waiting_ids:
            return results
        now = self.clock()
        try:
            cards = await self._my_cards([self.cfg.in_work_column_id])
        except KaitenError as exc:
            log.warning("Подсказка «в ожидание»: не смог прочитать «В работе»: %s", exc)
            return results
        for item in cards:
            # на 2-м уровне разговор идёт с клиентом — смотрим его чат, иначе чат автора
            key = ((await self._card_client_key(item)) if self.cfg.client_lanes else None) \
                or await self._card_author_key(item)
            if not key:
                continue
            chat = self.storage.find_chat(key) or {}
            ours = [chat.get("last_out_at")]  # ты сам с корпоративного аккаунта
            for row in self._contacts_for(item["id"]):  # и бот — этому же автору
                contact = normalize_contact(row["contact"])
                if contact and contact.key == key:
                    ours.append(self.storage.last_sent_to(row["contact"]))
            last_ours = max((t for t in ours if t), default=None)
            last_theirs = chat.get("last_in_at")
            if not last_ours or (last_theirs and last_theirs >= last_ours):
                continue  # мяч на нашей стороне или мы ещё не писали
            if now - last_ours < timedelta(minutes=self.cfg.wait_hint_min):
                continue
            episode = last_ours.isoformat()
            if self.storage.get(f"waithint:{item['id']}") == episode:
                continue
            self.storage.set(f"waithint:{item['id']}", episode)
            minutes = int((now - last_ours).total_seconds() // 60)
            rows = self._buttons(item["id"])
            rows.append([{"text": "➡️ В «Ждём ответа»", "callback_data": f"wait:{item['id']}"},
                         {"text": "Я ещё работаю", "callback_data": f"keep:{item['id']}"}])
            await self.alert.send(f"⏳ «{item.get('title') or item['id']}»: последнее сообщение наше, автор молчит "
                                  f"{minutes} мин. Перенести в «Ждём ответа»?", rows)
            results.append(f"hint:{item['id']}")
        return results

    def _contacts_for(self, card_id: int) -> list[dict]:
        rows = self.storage._exec("SELECT DISTINCT contact FROM outbox WHERE card_id = ? AND status = 'sent'",  # noqa: SLF001
                                  (card_id,))
        return [dict(r) for r in rows]

    async def mark_manual(self, card_id: int, role: str) -> int:
        """Кнопка «Отправил вручную»: отменить всё неотправленное по этой заявке этому адресату."""
        texts = self.storage.fail_group(card_id, role, "отправил вручную", "cancelled", ("pending", "held"))
        await self._drop_corp_card(card_id, role)
        if texts:
            self.note(card_id, f"ты отметил «Отправил вручную» — сообщение {self.WHO.get(role, role)} не шлю")
        return len(texts)

    # --- SLA ---
    async def check_sla(self) -> int:
        """Напоминание на warn_at и на дедлайне, пока карточка в колонке «В работе»."""
        alerts = 0
        if await self.is_paused():
            return alerts
        now = self.clock()
        for row in self.storage.open_cards():
            if self.assess.on and self.assess.state(row["card_id"]):
                continue  # ещё не в работе: срок SLA работы начнётся, когда возьмёшь её
            due_warn = not row["warn_sent"] and now >= row["warn_at"]
            due_breach = not row["breach_sent"] and now >= row["deadline"]
            if not (due_warn or due_breach):
                continue
            try:
                card = await self.kaiten.get_card(row["card_id"])
            except KaitenError as exc:
                log.warning("SLA: не смог проверить карточку %s: %s", row["card_id"], exc)
                continue
            if not card or card.get("column_id") != self.cfg.in_work_column_id:
                self.storage.mark_card(row["card_id"], closed=1)
                continue
            if due_breach:
                await self.alert.send(f"🔥 SLA просрочен: «{row['title']}» (срок был "
                                      f"{self._fmt(row['deadline'])})", self._buttons(row["card_id"]))
                self.storage.mark_card(row["card_id"], warn_sent=1, breach_sent=1, closed=1)
                self.note(row["card_id"], "SLA просрочен — напомнил тебе")
            else:
                left = max(int((row["deadline"] - now).total_seconds() // 60), 0)
                await self.alert.send(f"⏰ До SLA {left} мин: «{row['title']}» — ответить до "
                                      f"{self._fmt(row['deadline'])}", self._buttons(row["card_id"]))
                self.storage.mark_card(row["card_id"], warn_sent=1)
                self.note(row["card_id"], f"до SLA {left} мин — напомнил тебе")
            alerts += 1
        return alerts

    # --- «Проблема актуальна?» через 6–8 часов тишины ---
    async def followup_columns(self) -> tuple[list[int], str]:
        """Где ловить заявки для «актуально ли?».

        У вас Kaiten сам переносит карточку из «Ждём ответа» в «Повторно связаться» через 6 часов —
        тогда сигнал = карточка попала в «Повторно связаться» (режим "recontact"), а «Ждём ответа»
        не отслеживаем, чтобы не было двух напоминаний. Если такой колонки нет — ждём 6–8 ч в «Ждём ответа».
        """
        waiting = await self.waiting_column_ids()
        if self._recontact_ids:
            return self._recontact_ids, "recontact"
        return waiting, "waiting"

    async def waiting_column_ids(self) -> list[int]:
        """WAITING_COLUMN_IDS или колонка «Ждём ответа», найденная на доске сама (заодно «Повторно связаться»)."""
        if self._waiting_ids and (self._recontact_ids or self.storage.get("columns_discovered")
                                  or self.cfg.waiting_column_ids):
            return self._waiting_ids
        cached = self.storage.get("waiting_column_ids", "")
        if cached and self.storage.get("columns_discovered"):
            self._waiting_ids = [int(x) for x in cached.split(",") if x]
            recontact = self.storage.get("recontact_column_ids", "")
            if recontact and not self._recontact_ids:
                self._recontact_ids = [int(x) for x in recontact.split(",") if x]
            return self._waiting_ids
        now = self.clock()
        if self._waiting_lookup_at and now - self._waiting_lookup_at < timedelta(hours=1):
            return []
        self._waiting_lookup_at = now
        board_id = None
        try:
            for column_id in [self.cfg.in_work_column_id, *sorted(self.cfg.queue_column_ids)]:
                for item in await self.kaiten.list_cards(column_id, page_size=1, max_pages=1):
                    board_id = item.get("board_id")
                    break
                if board_id:
                    break
            board = await self.kaiten.get_board(board_id) if board_id else None
        except KaitenError as exc:
            log.warning("Не смог найти колонку «Ждём ответа»: %s", exc)
            return []
        found = self._waiting_ids or waiting_columns(board or {})
        if not self._recontact_ids:
            self._recontact_ids = recontact_columns(board or {}) or None
        if found:
            self._waiting_ids = found
            self.storage.set("waiting_column_ids", ",".join(map(str, found)))
            self.storage.set("recontact_column_ids", ",".join(map(str, self._recontact_ids or [])))
            self.storage.set("columns_discovered", "1")
            log.info("Колонки: «Ждём ответа» %s, «Повторно связаться» %s", found, self._recontact_ids)
        elif board_id and not self.storage.get("waiting_missing_alerted"):
            self.storage.set("waiting_missing_alerted", "1")
            await self.alert.send("⚠️ Не нашёл на доске колонку «Ждём ответа» — напоминания «актуально ли?» "
                                  "выключены. Укажи её id в .env: WAITING_COLUMN_IDS=…")
        return found

    async def poll_waiting(self) -> list[str]:
        """Карточки в «Ждём ответа», где ты ответственный: одно напоминание после 6–8 ч тишины в чате."""
        results: list[str] = []
        if not self.cfg.followup_after_h or await self.is_paused():
            return results
        columns, mode = await self.followup_columns()
        if not columns:
            return results
        now = self.clock()
        first_run = not self.storage.get("followup_enabled_at")
        if first_run:
            # Карточки, которые уже ждали ответа до включения функции, не трогаем — никакой рассылки разом.
            self.storage.set("followup_enabled_at", now.isoformat())
        enabled_at = datetime.fromisoformat(self.storage.get("followup_enabled_at"))
        low, high = self.cfg.followup_after_h
        seen: set[int] = set()
        complete = True
        for column_id in columns:
            try:
                items = await self.kaiten.list_cards(column_id)
            except KaitenError as exc:
                log.warning("Опрос «Ждём ответа» не удался: %s", exc)
                complete = False
                continue
            for item in items:
                card_id = item.get("id")
                if not isinstance(card_id, int):
                    continue
                if ("members" in item or "responsible_id" in item) and not is_responsible(item, self.cfg.my_user_id):
                    continue
                seen.add(card_id)
                moved = moved_at(item)
                row = self.storage.waiting_row(card_id)
                if row is None or (moved and moved > row["since"] + timedelta(seconds=1)):
                    since = moved or now
                    done = first_run or since < enabled_at
                    self.storage.set_waiting(card_id, since, self.rng.uniform(low, high) * 3600, done)
                    self._followup_next_check.pop(card_id, None)
                    row = self.storage.waiting_row(card_id)
                if row["done"]:
                    continue
                results.append(await self._maybe_followup(card_id, row, now, mode))
        if complete:
            self.storage.forget_waiting_except(seen)
        return results

    async def _maybe_followup(self, card_id: int, row: dict, now: datetime, mode: str = "waiting") -> str:
        # delay — сколько нужно тишины в чате; base — сколько карточка должна пролежать в колонке.
        # «Повторно связаться»: Kaiten уже выждал 6 ч, поэтому base = 0.
        delay = timedelta(seconds=row["delay_s"])
        base = timedelta(0) if mode == "recontact" else delay
        next_check = self._followup_next_check.get(card_id, row["since"] + base)
        if now < next_check:
            return "followup:wait"
        try:
            card = await self.kaiten.get_card(card_id)
        except KaitenError:
            return "followup:error"
        if not card or not is_responsible(card, self.cfg.my_user_id):
            return "followup:not-mine"
        raw, problem = await self.author_contact(card)
        title = str(card.get("title") or f"Заявка {card_id}")
        if not raw or normalize_contact(raw) is None:
            self.storage.mark_waiting_done(card_id)
            await self.alert.send(f"🔁 «{title}» ждёт ответа уже {delay.total_seconds() / 3600:.0f} ч, "
                                  f"но я не знаю, кому напомнить ({problem or raw}). Напомни вручную.",
                                  self._buttons(card_id))
            return "followup:no-contact"
        contact = normalize_contact(raw)
        chat = self.storage.find_chat(contact.key)
        activity = [self.storage.last_sent_to(raw)]
        if chat:
            activity += [chat.get("last_in_at"), chat.get("last_out_at")]
        chat_last = max((a for a in activity if a), default=None)
        last = max(row["since"], chat_last) if chat_last else row["since"]
        due = max(row["since"] + base, chat_last + delay if chat_last else row["since"] + base)
        if now < due:  # в чате была переписка — отсчитываем тишину заново
            self._followup_next_check[card_id] = due
            return "followup:chat-active"
        book = await self.texts()
        send_at = self._send_time()
        deadline = row["since"] + delay
        rendered = book.render("followup", self._values(card_id, title, deadline, send_at),
                               self._last_variant("followup", contact.key))
        self.storage.mark_waiting_done(card_id)
        if not rendered:
            return "followup:disabled"
        bubbles, variant = rendered
        self.storage.set(f"last_variant:followup:{contact.key}", str(variant))
        series = self._new_series(card_id, FOLLOWUP_ROLE)
        for position, text in enumerate(bubbles):
            self.storage.enqueue(card_id, FOLLOWUP_ROLE, raw, text,
                                 send_at + timedelta(seconds=position * BUBBLE_GAP_S), now, series)
        when = "сейчас" if send_at - now < timedelta(minutes=30) else self._fmt(send_at)
        self.note(card_id, f"запланировал «актуально ли?» для {raw} ({when})")
        quiet = int((now - chat_last).total_seconds() // 3600) if chat_last else None
        why = "попала в «Повторно связаться»" if mode == "recontact" else f"{int((now - last).total_seconds() // 3600)} ч тишины"
        if quiet is not None and mode == "recontact":
            why += f", в чате тихо {quiet} ч"
        if when == "сейчас":
            plan = f"⏳ Через пару минут спрошу {raw}, актуально ли (пока НЕ отправлено)."
        else:
            plan = (f"⏰ Сейчас нерабочее время — пока НИЧЕГО не отправил. Спрошу {raw} "
                    f"{self._fmt(send_at)}, когда начнётся рабочий день.")
        await self.alert.send(f"🔁 «{title}»: {why}.\n{plan}\nТекст: {' ⏎ '.join(bubbles)}\n"
                              "Не отправлю, если автор ответит, ты напишешь сам или заявку закроют.",
                              self._buttons(card_id, raw, FOLLOWUP_ROLE))
        return "followup:queued"

    def _last_variant(self, kind: str, key: str) -> int | None:
        value = self.storage.get(f"last_variant:{kind}:{key}")
        return int(value) if value else None

    # --- сводки ---
    def status_text(self, connected: bool | None = None) -> str:
        paused = self.storage.get("paused_until", "") or ""
        pause = ("нет" if not paused else "до /resume" if paused == "forever"
                 else f"до {self._fmt(datetime.fromisoformat(paused))}")
        day = self.stats(timedelta(hours=24))
        lines = [
            f"✅ Работаю с {self._fmt(self.started_at)}" + (" (ТЕСТОВЫЙ режим)" if self.cfg.dry_run else ""),
            f"Пауза: {pause}",
            f"Рабочее время для сообщений: {self.cfg.work_start:%H:%M}–{self.cfg.work_end:%H:%M}",
            f"Темп: {self.pace().describe()} (/speed — сменить)",
            ("Время реакции: до «В работу» после оценки (REACTION_MODE=assess), ждут оценки: "
             f"{len(self.assess.waiting_cards())}" if self.assess.on
             else "Время реакции: бот сам переносит в «В работе» (REACTION_MODE=classic)"),
        ]
        if self._off_shift():
            lines.append(f"🌙 Сейчас вне смены: новые заявки сам не беру, пришлю кнопку «Взять сейчас»; "
                         f"возьму в {self.cfg.work_start:%H:%M}. Сообщения с общего аккаунта сейчас пишет "
                         "дежурный — твои заявки по ним не двигаю")
        if connected is not None:
            lines.append("Бот подключён к корпоративному аккаунту: " + ("да" if connected else "НЕТ"))
        lines += [
            f"В очереди на отправку: {len(self.storage.groups_with_status('pending'))}, "
            f"жду, пока человек напишет: {len(self.storage.groups_with_status('held'))}",
            f"За сутки: {day}",
        ]
        return "\n".join(lines)

    def stats(self, period: timedelta) -> str:
        data = self.storage.stats_since(self.clock() - period)
        by = data["by_status"]
        return (f"взял заявок {data['taken']}, написал людям {by.get('sent', 0)}, "
                f"напоминаний {data['followups']}, не смог — {by.get('failed', 0)}, "
                f"отменил (ты написал сам / заявку забрали) — {by.get('cancelled', 0)}")

    async def heartbeat(self) -> bool:
        """Раз в день в начале рабочего времени: «работаю» + цифры за прошедшие сутки."""
        if not self.cfg.heartbeat:
            return False
        local = self.clock().astimezone(self.cfg.tz)
        today = local.date().isoformat()
        if local.time() < self.cfg.work_start or self.storage.get("heartbeat_date") == today:
            return False
        self.storage.set("heartbeat_date", today)
        paused = " Сейчас на паузе — /resume, чтобы продолжить." if self.storage.get("paused_until") else ""
        await self.alert.send(f"☀️ {greeting(local)}! Сервис работает.{paused}\n"
                              f"За сутки: {self.stats(timedelta(hours=24))}")
        return True

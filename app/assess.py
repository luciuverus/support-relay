"""Время реакции = когда специалист реально взял заявку в работу (решение созвона 09.10.2026).

REACTION_MODE=assess:
1. Заявка назначена на тебя → бот здоровается, но карточка остаётся в «Новой»: время реакции идёт.
2. Не хватает информации, чтобы оценить заявку → быстрый ответ «ждём ответа» (или кнопка «⏸ Ждём инфо»)
   переносит карточку в «Ожидание информации для оценки» — там время реакции на паузе.
3. Клиент ответил → бот сам возвращает карточку в «Новую», время реакции снова идёт.
4. Информации хватает и фактическая категория заполнена → кнопка «▶️ В работу» (или /work номер):
   бот переносит карточку в «В работе» и пишет клиенту ориентировочное время решения по SLA.

REACTION_MODE=classic — всё как раньше: бот сам переносит заявку в «В работе».
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .kaiten import KaitenError
from .logic import client_contact, client_contact_from_text, column_title, flatten_columns, is_responsible, \
    normalize_contact, warn_time

log = logging.getLogger(__name__)

WORK_ROLE = "work"
PREFIX = "assess:"
WAIT_TITLE_WORDS = ("оценк",)            # «Ожидание информации для оценки», «Ждём инфо для оценки»
DEFAULT_WORK_TEXT = ("Спасибо, информации достаточно — взяли заявку {card_id} в работу. "
                     "Ориентировочное время решения — {eta}.")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def eta_phrase(minutes: int) -> str:
    """40 → «40 минут», 60 → «1 час», 90 → «1 час 30 минут», 240 → «4 часа»."""
    minutes = max(int(minutes), 1)
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} {_plural(hours, 'час', 'часа', 'часов')}")
    if rest or not hours:
        parts.append(f"{rest} {_plural(rest, 'минута', 'минуты', 'минут')}")
    return " ".join(parts)


class Assess:
    def __init__(self, relay):
        self.relay = relay
        self.cfg = relay.cfg
        self.storage = relay.storage
        self.kaiten = relay.kaiten
        self._wait_ids: list[int] | None = sorted(self.cfg.assess_wait_column_ids) or None
        self._wait_lookup_at: datetime | None = None

    @property
    def on(self) -> bool:
        return self.cfg.reaction_mode == "assess"

    @property
    def alert(self):
        return self.relay.alert

    # --- состояние: карточки, которые ждут оценки ---
    def state(self, card_id: int) -> dict | None:
        raw = self.storage.get(f"{PREFIX}{card_id}", "")
        return json.loads(raw) if raw else None

    def _save(self, card_id: int, state: dict) -> None:
        self.storage.set(f"{PREFIX}{card_id}", json.dumps(state, ensure_ascii=False))

    def drop(self, card_id: int) -> None:
        self.storage.set(f"{PREFIX}{card_id}", "")

    def waiting_cards(self) -> list[int]:
        return sorted(int(k[len(PREFIX):]) for k in self.storage.with_prefix(PREFIX) if k[len(PREFIX):].isdigit())

    def begin(self, card: dict, now: datetime) -> None:
        self._save(card["id"], {"since": now.isoformat(), "title": str(card.get("title") or card["id"]),
                                "origin": card.get("column_id"), "reminded": False})

    # --- колонка «Ожидание информации для оценки» ---
    async def wait_columns(self) -> list[int]:
        if self._wait_ids:
            return self._wait_ids
        cached = self.storage.get("assess_wait_ids", "")
        if cached:
            self._wait_ids = [int(x) for x in cached.split(",") if x]
            return self._wait_ids
        now = self.relay.clock()
        if self._wait_lookup_at and now - self._wait_lookup_at < timedelta(hours=1):
            return []
        self._wait_lookup_at = now
        board_id = self.cfg.in_work_board_id
        try:
            if not board_id:
                for column_id in [self.cfg.in_work_column_id, *sorted(self.cfg.queue_column_ids)]:
                    for item in await self.kaiten.list_cards(column_id, page_size=1, max_pages=1):
                        board_id = item.get("board_id")
                        break
                    if board_id:
                        break
            board = await self.kaiten.get_board(board_id) if board_id else None
        except KaitenError as exc:
            log.warning("Не смог найти колонку «Ожидание информации для оценки»: %s", exc)
            return []
        found = [c["id"] for c in flatten_columns(board or {}) if isinstance(c.get("id"), int)
                 and any(w in column_title(c).casefold() for w in WAIT_TITLE_WORDS)]
        if found:
            self._wait_ids = found
            self.storage.set("assess_wait_ids", ",".join(map(str, found)))
        return found

    # --- фактическая категория и срок ---
    async def category(self, card: dict) -> tuple[bool, str] | None:
        """(заполнена ли, текст). None — не знаю, какое поле категория (тогда не проверяем)."""
        quick = self.relay.quick
        try:
            fields = await quick.fields()
        except KaitenError as exc:
            log.warning("Категория: не смог прочитать поля Kaiten: %s", exc)
            return None
        if not fields:
            return None
        value = (card.get("properties") or {}).get(fields[0])
        ids = value if isinstance(value, list) else [value]
        ids = [v for v in ids if v not in (None, "")]
        if not ids:
            return False, ""
        names = []
        try:
            options = {o["id"]: o["value"] for o in await quick.options()}
        except KaitenError:
            options = {}
        for v in ids:
            if isinstance(v, dict):
                v = v.get("id", v.get("value"))
            names.append(str(options.get(v, options.get(int(v), v)) if str(v).isdigit() else v))
        return True, ", ".join(names)

    def eta_minutes(self, category: str) -> int:
        low = (category or "").casefold()
        for name, minutes in self.cfg.assess_eta_by_category:
            if name and (name == low or name in low):
                return minutes
        return self.cfg.assess_eta_default_min

    async def _main_contact(self, card: dict) -> tuple[str, str]:
        """Кому писать время решения: клиенту на уровнях CLIENT_LANES, иначе автору. (контакт, кто)."""
        relay = self.relay
        if await relay._client_lane(card):
            key = await relay.field_key(self.cfg.client_tg_field)
            raw = client_contact_from_text(client_contact(card, key) if key else "")
            if raw:
                return raw, "клиенту"
        raw, _ = await relay.author_contact(card)
        return raw, "автору"

    # --- «▶️ В работу» ---
    async def start_work(self, card_id: int, eta_only: bool = False) -> str:
        relay = self.relay
        try:
            card = await self.kaiten.get_card(card_id)
        except KaitenError as exc:
            await self.alert.send(f"❗ Kaiten не ответил ({exc}) — перенеси №{card_id} в «В работе» вручную.",
                                  relay._buttons(card_id))
            return "Ошибка Kaiten"
        if not card or not is_responsible(card, self.cfg.my_user_id):
            self.drop(card_id)
            return "Заявка уже не на тебе"
        column = card.get("column_id")
        title = str(card.get("title") or card_id)
        allowed = set(self.cfg.queue_column_ids) | set(await self.wait_columns())
        if column == self.cfg.in_work_column_id and not eta_only:
            self.drop(card_id)
            return "Уже в работе"
        if column != self.cfg.in_work_column_id and column not in allowed:
            self.drop(card_id)
            return "Карточка уже не в «Новой»"
        cat = await self.category(card)
        if cat is not None and not cat[0] and self.cfg.assess_require_category and not eta_only:
            rows = relay._buttons(card_id)
            rows.append([{"text": "🔁 Заполнил — в работу", "callback_data": f"go:{card_id}"}])
            await self.alert.send(f"📝 «{title}» (№{card_id}): сначала заполни в Kaiten «Фактическая категория "
                                  "проблемы» — по ней считается SLA. Потом нажми кнопку ещё раз.", rows)
            return "Сначала категория"
        minutes = self.eta_minutes(cat[1] if cat else "")
        now = relay.clock()
        if column != self.cfg.in_work_column_id:
            try:
                if not self.cfg.dry_run:
                    await self.kaiten.move_card(card_id, self.cfg.in_work_column_id, self.cfg.in_work_board_id)
            except KaitenError as exc:
                await self.alert.send(f"❗ Не смог перенести «{title}» в «В работе»: {exc}. Перенеси вручную.",
                                      relay._buttons(card_id))
                return "Ошибка Kaiten"
        self.drop(card_id)
        deadline = now + timedelta(minutes=minutes)
        warn_at = warn_time(now, deadline, self.cfg.sla_warn_share)
        if relay.storage.has_card(card_id):
            relay.storage.set_deadline(card_id, deadline, warn_at)
        else:
            relay.storage.claim_card(card_id, title, f"{self.cfg.card_url_prefix}{card_id}", now, deadline, warn_at)
        sent_to = await self._queue_eta(card, card_id, title, deadline, minutes)
        test = "[тест, в Kaiten не перенёс] " if self.cfg.dry_run else ""
        where = f"{sent_to[1]} {sent_to[0]}" if sent_to else ""
        relay.note(card_id, "[тест] " * self.cfg.dry_run + f"в работе по команде — время решения "
                   f"{eta_phrase(minutes)}" + (f", пишу {where}" if where else ", писать некому"))
        text = (f"▶️ {test}«{title}» в работе. "
                + (f"Пишу {where}: ориентировочное время решения — {eta_phrase(minutes)}. "
                   if where else "Кому написать время решения, не знаю — напиши сам. ")
                + f"SLA работы до {relay._fmt(deadline)}.")
        if cat is None:
            text += "\n⚠️ Не понял, какое поле — фактическая категория (FACT_CATEGORY_FIELD), поэтому не проверял её."
        await self.alert.send(text, relay._buttons(card_id))
        return "В работе"

    async def _queue_eta(self, card: dict, card_id: int, title: str, deadline: datetime,
                         minutes: int) -> tuple[str, str] | None:
        relay = self.relay
        raw, who = await self._main_contact(card)
        if not raw or normalize_contact(raw) is None:
            return None
        key = normalize_contact(raw).key
        book = await relay.texts()
        send_at = relay._greeting_time()
        values = relay._values(card_id, title, deadline, send_at)
        values["eta"] = eta_phrase(minutes)
        last = relay.storage.get(f"last_variant:work_started:{key}")
        rendered = book.render("work_started", values, int(last) if last else None) \
            if book.enabled("work_started") else ([DEFAULT_WORK_TEXT.format(**values)], 0)
        if rendered is None:
            return None
        bubbles, variant = rendered
        relay.storage.set(f"last_variant:work_started:{key}", str(variant))
        series = relay._new_series(card_id, WORK_ROLE)
        for position, text in enumerate(bubbles):
            relay.storage.enqueue(card_id, WORK_ROLE, raw, text, send_at + timedelta(seconds=position), relay.clock(),
                                  series)
        return raw, who

    # --- «⏸ Ждём информацию для оценки» ---
    async def to_info_wait(self, card_id: int) -> bool:
        relay = self.relay
        wait = await self.wait_columns()
        if not wait:
            await self.alert.send("⚠️ На доске нет колонки «Ожидание информации для оценки» (или укажи её id в "
                                  ".env: ASSESS_WAIT_COLUMN_IDS). Карточка осталась в «Новой» — время реакции идёт.",
                                  relay._buttons(card_id))
            return False
        moved = await relay._move_if(card_id, set(self.cfg.queue_column_ids), wait[0])
        if not moved:
            return False
        state = self.state(card_id) or {}
        state.update({"in_wait": relay.clock().isoformat(), "reminded": False})
        self._save(card_id, state)
        relay.note(card_id, "[тест] " * self.cfg.dry_run + "перенёс в «Ожидание информации для оценки»")
        test = "[тест, в Kaiten не перенёс] " if self.cfg.dry_run else ""
        rows = relay._buttons(card_id)
        rows.append([{"text": "▶️ В работу", "callback_data": f"go:{card_id}"}])
        await self.alert.send(f"⏸ {test}«{state.get('title') or card_id}» — в «Ожидание информации для оценки»: "
                              "время реакции на паузе. Ответит клиент — верну в «Новую».", rows)
        return True

    async def quick_wait(self, keyset: set[str]) -> bool:
        """Быстрый ответ «ждём ответа» по заявке, которую ещё не брали в работу. True — обработали."""
        if not self.on:
            return False
        cards = [c for c in await self.relay.quick._cards(keyset, sorted(self.cfg.queue_column_ids))
                 if self.state(c["id"])]
        if not cards:
            return False
        if len(cards) > 1:
            rows = [[{"text": f"Ждём инфо: {str(c.get('title') or c['id'])[:40]}", "callback_data": f"info:{c['id']}"}]
                    for c in cards[:6]]
            await self.alert.send("⏸ У этого человека несколько заявок в «Новой». По какой ждём информацию?", rows)
            return True
        await self.to_info_wait(cards[0]["id"])
        return True

    # --- клиент ответил → обратно в «Новую» ---
    async def on_client_message(self, keyset: set[str]) -> list[int]:
        if not self.on:
            return []
        wait = await self.wait_columns()
        if not wait:
            return []
        relay = self.relay
        moved = []
        try:
            cards = await relay._my_cards(wait)
            for item in cards:
                if not (await relay.card_keys(item)) & keyset:
                    continue
                state = self.state(item["id"]) or {"title": str(item.get("title") or item["id"])}
                target = state.get("origin") if state.get("origin") in self.cfg.queue_column_ids \
                    else min(self.cfg.queue_column_ids)
                if not await relay._move_if(item["id"], set(wait), target):
                    continue
                state.update({"since": relay.clock().isoformat(), "reminded": False})
                state.pop("in_wait", None)
                self._save(item["id"], state)
                relay.note(item["id"], "[тест] " * self.cfg.dry_run + "вернул в «Новую» — клиент ответил")
                rows = relay._buttons(item["id"])
                rows.append([{"text": "▶️ В работу", "callback_data": f"go:{item['id']}"},
                             {"text": "⏸ Ещё ждём", "callback_data": f"info:{item['id']}"}])
                test = "[тест, в Kaiten не перенёс] " if self.cfg.dry_run else ""
                await self.alert.send(f"💬 {test}По «{state['title']}» ответили — вернул в «Новую», время реакции "
                                      "снова идёт. Хватает информации — «▶️ В работу».", rows)
                moved.append(item["id"])
        except KaitenError as exc:
            log.warning("Ответ клиента: не смог проверить карточки в ожидании оценки: %s", exc)
        return moved

    # --- раз в опрос: напоминания и уборка ---
    async def poll(self) -> list[str]:
        if not self.on:
            return []
        relay = self.relay
        results = []
        now = relay.clock()
        quiet = await relay.is_paused() or relay._off_shift()
        wait = set(await self.wait_columns())
        for card_id in self.waiting_cards():
            state = self.state(card_id)
            if not state:
                continue
            try:
                card = await self.kaiten.get_card(card_id)
            except KaitenError:
                results.append(f"assess:error:{card_id}")
                continue
            if not card or not is_responsible(card, self.cfg.my_user_id):
                self.drop(card_id)
                results.append(f"assess:not-mine:{card_id}")
                continue
            column = card.get("column_id")
            if column == self.cfg.in_work_column_id:
                # перенёс в работу сам, прямо в Kaiten — срок SLA считаем от этого момента
                self.drop(card_id)
                cat = await self.category(card)
                minutes = self.eta_minutes(cat[1] if cat else "")
                deadline = now + timedelta(minutes=minutes)
                if relay.storage.has_card(card_id):
                    relay.storage.set_deadline(card_id, deadline, warn_time(now, deadline, self.cfg.sla_warn_share))
                relay.note(card_id, "ты взял в работу сам в Kaiten — время решения клиенту не отправлял")
                rows = relay._buttons(card_id)
                rows.append([{"text": "📨 Отправить время решения", "callback_data": f"eta:{card_id}"}])
                await self.alert.send(f"▶️ «{state.get('title') or card_id}» ты взял в работу сам. Отправить "
                                      f"клиенту ориентировочное время решения ({eta_phrase(minutes)})?", rows)
                results.append(f"assess:manual-work:{card_id}")
                continue
            if column in wait:
                results.append(f"assess:info-wait:{card_id}")
                continue
            if column not in self.cfg.queue_column_ids:
                self.drop(card_id)
                relay.storage.mark_card(card_id, closed=1)
                results.append(f"assess:gone:{card_id}")
                continue
            since = datetime.fromisoformat(state["since"])
            if quiet or state.get("reminded") or not self.cfg.assess_remind_min \
                    or now - since < timedelta(minutes=self.cfg.assess_remind_min):
                results.append(f"assess:new:{card_id}")
                continue
            state["reminded"] = True
            self._save(card_id, state)
            minutes = int((now - since).total_seconds() // 60)
            rows = relay._buttons(card_id)
            rows.append([{"text": "▶️ В работу", "callback_data": f"go:{card_id}"},
                         {"text": "⏸ Ждём инфо", "callback_data": f"info:{card_id}"}])
            await self.alert.send(f"⏱ «{state.get('title') or card_id}» в «Новой» уже {minutes} мин — время реакции "
                                  "идёт. Хватает информации — «▶️ В работу», ждёшь клиента — «⏸ Ждём инфо».", rows)
            results.append(f"assess:reminded:{card_id}")
        return results

    def pick_text(self) -> tuple[str, list[list[dict]] | None]:
        """/work без номера: какие заявки ждут оценки."""
        ids = self.waiting_cards()
        if not ids:
            return "Заявок, которые ждут оценки, нет.", None
        rows = [[{"text": f"▶️ {str((self.state(i) or {}).get('title') or i)[:40]}", "callback_data": f"go:{i}"}]
                for i in ids[:8]]
        return "Какую заявку взять в работу?", rows

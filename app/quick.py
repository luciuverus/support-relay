"""Быстрые ответы корпоративного аккаунта → действия в Kaiten.

- «ждём ответ» (/Ожидаем): карточку автора из «В работе» — в «Ждём ответа»;
- «закрываем заявку?» (/закрываем) и «оцените работу» (/Оценкаработы): Kaiten не закроет заявку
  с пустой «Фактической категорией проблемы» — бот спрашивает тебя кнопками и заполняет поля.

Бот видит только текст сообщения, не название быстрого ответа. Поэтому тексты можно «показать» боту:
/learn wait (close, rate) — следующее сообщение, которое ты отправишь сам, станет триггером.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter

from .kaiten import KaitenError
from .logic import column_title, flatten_columns
from .similar import card_query

log = logging.getLogger(__name__)

KINDS = {"wait": "«ждём ответ»", "close": "«закрываем заявку»", "rate": "«оценка работы»",
         "noreply": "«нет ответа — закрываем»"}
DEFAULT_TRIGGERS = {
    "noreply": ["не получили от вас ответа", "вынуждены закрыть заявку"],
    "wait": ["ждем от вас обратную связь", "ожидаем вашего ответа", "ждем вашего ответа", "ждем ответа", "ожидаем ответа", "ждем обратной связи"],
    "close": ["закрываем заявку", "можно закрывать заявку", "заявку можно закрыть"],
    "rate": ["оцените качество работы специалиста", "оцените", "оценку работы", "оценить работу", "оценка работы", "оцените нашу работу"],
}
PRIORITY = ("noreply", "rate", "close", "wait")
# Категория и итог для «клиент не ответил»
NOREPLY_CATEGORY_WORDS = ("неактуал", "решилось само", "нет ответа", "не ответил")
NOREPLY_SOLUTION = "Клиент не ответил, заявка закрыта по регламенту"
DONE_NAMES = ("готово", "выполнено", "выполнена", "done", "закрыто", "закрыта", "решено")
FACT_HINT = "фактическ"
MIN_TRIGGER = 6


def norm(text: str) -> str:
    text = str(text or "").casefold().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _filled(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict, str)):
        return len(value) > 0 and (not isinstance(value, str) or bool(value.strip()))
    return True


class QuickReplies:
    def __init__(self, relay):
        self.relay = relay
        self.storage = relay.storage
        self.kaiten = relay.kaiten
        self.cfg = relay.cfg
        self._fields: tuple[str, str] | None = None     # (ключ категории, ключ «что сделали»)
        self._options: list[dict] | None = None         # варианты категории [{id, value}]
        self._shape: str | None = None                  # "list" | "scalar" — как Kaiten хранит выбор
        self._done: int | None = None
        self._board_hint: int | None = None             # доска последней карточки, с которой работали
        self._field_alerted = False

    @property
    def alert(self):
        return self.relay.alert

    # --- триггеры ---
    def learned(self) -> dict[str, list[str]]:
        raw = self.storage.get("quick_triggers", "")
        data = json.loads(raw) if raw else {}
        return {k: list(data.get(k, [])) for k in KINDS}

    def triggers(self) -> dict[str, list[str]]:
        learned = self.learned()
        return {k: [norm(t) for t in [*learned[k], *DEFAULT_TRIGGERS[k]] if len(norm(t)) >= MIN_TRIGGER]
                for k in KINDS}

    def kind_of(self, text: str) -> str | None:
        body = norm(text)
        if not body:
            return None
        found = self.triggers()
        for kind in PRIORITY:
            if any(t in body for t in found[kind]):
                return kind
        return None

    def start_learning(self, kind: str) -> str:
        self.storage.set("learn_trigger", kind)
        return (f"Хорошо. Отправь сейчас кому-нибудь быстрый ответ {KINDS[kind]} (или напиши такое сообщение сам) "
                "с корпоративного аккаунта — запомню его текст и сразу выполню действие.")

    def reset_learned(self) -> str:
        self.storage.set("quick_triggers", "")
        self.storage.set("learn_trigger", "")
        return "Забыл выученные тексты, остались стандартные."

    def list_text(self) -> str:
        learned = self.learned()
        lines = ["Быстрые ответы, на которые я реагирую:"]
        for kind, title in KINDS.items():
            mine = learned[kind]
            lines.append(f"{title}: " + ("; ".join(f"«{t[:50]}»" for t in mine) if mine else "только стандартные"))
        lines.append("Научить: /learn wait, /learn close, /learn rate, /learn noreply. Сбросить: /learn reset")
        return "\n".join(lines)

    async def _learn(self, kind: str, text: str) -> None:
        phrase = norm(text)[:80]
        if len(phrase) < MIN_TRIGGER:
            await self.alert.send("Слишком короткий текст — не запомнил. Попробуй ещё раз: /learn " + kind)
            return
        data = self.learned()
        if phrase not in data[kind]:
            data[kind].append(phrase)
        for other in KINDS:  # одна фраза — одно действие
            if other != kind and phrase in data[other]:
                data[other].remove(phrase)
        self.storage.set("quick_triggers", json.dumps(data, ensure_ascii=False))
        await self.alert.send(f"🧠 Запомнил: «{text.strip()[:80]}» = {KINDS[kind]}.")

    # --- вход: ты сам написал в чат ---
    async def on_manual(self, keyset: set[str], text: str) -> bool:
        """True — это быстрый ответ, автоперенос «В работу» не нужен (ждём ответ / закрываем заявку)."""
        learning = self.storage.get("learn_trigger", "") or ""
        if learning in KINDS and text.strip():
            self.storage.set("learn_trigger", "")
            await self._learn(learning, text)
            kind = learning
        else:
            kind = self.kind_of(text)
        if kind is None:
            return False
        try:
            if kind == "wait":
                await self.to_waiting(keyset)
                return True
            if kind == "noreply":
                await self.noreply(keyset)
                return True
            await self.check_fields(keyset, after_rate=(kind == "rate"))
        except KaitenError as exc:
            log.warning("Быстрый ответ %s: %s", kind, exc)
            await self.alert.send(f"❗ Быстрый ответ {KINDS[kind]}: Kaiten не ответил ({exc}). Сделай вручную.")
        return True

    async def _cards(self, keyset: set[str], columns: list[int]) -> list[dict]:
        found = []
        for item in await self.relay._my_cards(columns):
            if (await self.relay.card_keys(item)) & keyset:
                found.append(item)
        return found

    # --- «ждём ответ» ---
    async def to_waiting(self, keyset: set[str]) -> None:
        # REACTION_MODE=assess: заявку ещё не брали в работу — «ждём ответа» = ждём информацию для оценки
        if await self.relay.assess.quick_wait(keyset):
            return
        await self.relay.waiting_column_ids()
        if not self.relay._waiting_ids:
            await self.alert.send("⚠️ Не нашёл колонку «Ждём ответа» — перенеси вручную.")
            return
        cards = await self._cards(keyset, [self.cfg.in_work_column_id])
        if len(cards) > 1:
            rows = [[{"text": f"В ожидание: {str(c.get('title') or c['id'])[:40]}", "callback_data": f"wait:{c['id']}"}]
                    for c in cards[:6]]
            await self.alert.send("⏸ У этого автора несколько заявок в «В работе». Какую перенести в «Ждём ответа»?",
                                  rows)
            return
        for item in cards:
            if await self.relay.move_to_waiting(item["id"]):
                test = "[тест, в Kaiten не перенёс] " if self.cfg.dry_run else ""
                rows = self.relay._buttons(item["id"])
                rows.append([{"text": "↩️ Вернуть в работу", "callback_data": f"work:{item['id']}"}])
                await self.alert.send(f"⏸ {test}Перенёс «{item.get('title') or item['id']}» в «Ждём ответа» — "
                                      "ты написал, что ждём ответ.", rows)

    # --- фактические поля ---
    async def fields(self) -> tuple[str, str] | None:
        if self._fields:
            return self._fields
        cat_spec = (self.cfg.fact_category_field or "").strip()
        sol_spec = (self.cfg.fact_solution_field or "").strip()
        props = await self.kaiten.list_custom_properties()
        facts = [p for p in props if FACT_HINT in str(p.get("name", "")).casefold() and p.get("id") is not None]

        def pick(spec: str, types: tuple[str, ...]) -> dict | None:
            if spec:
                direct = re.fullmatch(r"(?:id_)?(\d+)", spec)
                for p in props:
                    if (direct and str(p.get("id")) == direct.group(1)) or \
                            str(p.get("name", "")).strip().casefold() == spec.casefold():
                        return p
                return None
            typed = [p for p in facts if str(p.get("type", "")).casefold() in types]
            return typed[0] if len(typed) == 1 else None

        cat = pick(cat_spec, ("select", "catalog", "multi_select"))
        sol = pick(sol_spec, ("string", "text"))
        if not cat or not sol:
            if not self._field_alerted:
                self._field_alerted = True
                if not self.relay.is_primary:
                    return None  # .env правит главный — он получит это предупреждение сам
                names = ", ".join(f"«{p['name']}» ({p.get('type')})" for p in facts) or "нет ни одного"
                await self.alert.send("⚠️ Не понял, какие поля Kaiten — «Фактическая категория проблемы». "
                                      f"Нашёл: {names}. Укажи в .env FACT_CATEGORY_FIELD и FACT_SOLUTION_FIELD "
                                      "(название или id).")
            return None
        self._fields = (f"id_{cat['id']}", f"id_{sol['id']}")
        return self._fields

    async def options(self) -> list[dict]:
        if self._options is None:
            fields = await self.fields()
            if not fields:
                return []
            raw = await self.kaiten.list_select_values(int(fields[0][3:]))
            self._options = [{"id": o["id"], "value": str(o.get("value") or o.get("name") or "").strip()}
                             for o in raw if isinstance(o, dict) and o.get("id") is not None
                             and not o.get("deleted")]
        return self._options

    async def done_column(self, board_id: int | None = None) -> int | None:
        """Колонка «Готово». Доску берём из самой карточки (так работает и при пустой «В работе»)."""
        if self._done is not None:
            return self._done
        candidates = [board_id, self._board_hint, self.cfg.in_work_board_id]
        for column_id in [self.cfg.in_work_column_id, *sorted(self.cfg.queue_column_ids)]:
            if any(candidates):
                break
            for item in await self.kaiten.list_cards(column_id, page_size=1, max_pages=1):
                candidates.append(item.get("board_id"))
        for candidate in dict.fromkeys(c for c in candidates if c):
            board = await self.kaiten.get_board(candidate)
            for col in flatten_columns(board or {}):
                if column_title(col).casefold() in DONE_NAMES and isinstance(col.get("id"), int):
                    self._done = col["id"]
                    return self._done
        return None

    def _remember_board(self, card: dict | None) -> None:
        if card and card.get("board_id"):
            self._board_hint = card["board_id"]

    async def value_shape(self) -> str:
        """Как Kaiten хранит выбранную категорию на уже заполненных карточках: [id] или id."""
        if self._shape:
            return self._shape
        self._shape = "list"
        fields = await self.fields()
        done = await self.done_column()
        if fields and done:
            for item in await self.kaiten.list_cards(done, page_size=50, max_pages=1):
                value = (item.get("properties") or {}).get(fields[0])
                if _filled(value):
                    self._shape = "list" if isinstance(value, list) else "scalar"
                    break
        return self._shape

    def _state(self, card_id: int) -> dict:
        raw = self.storage.get(f"fill:{card_id}", "")
        return json.loads(raw) if raw else {}

    def _save_state(self, card_id: int, state: dict) -> None:
        self.storage.set(f"fill:{card_id}", json.dumps(state, ensure_ascii=False))

    async def check_fields(self, keyset: set[str], after_rate: bool) -> None:
        await self.relay.waiting_column_ids()
        columns = [self.cfg.in_work_column_id, *(self.relay._waiting_ids or []), *(self.relay._recontact_ids or [])]
        cards = await self._cards(keyset, columns)
        if len(cards) > 1:
            # в кнопке помним, была ли уже оценка: тогда выбранную заявку нужно закрыть
            rows = [[{"text": f"Заполнить: {str(c.get('title') or c['id'])[:40]}",
                      "callback_data": f"fill:{c['id']}:{int(after_rate)}"}]
                    for c in cards[:6]]
            await self.alert.send("📝 У этого автора несколько твоих заявок. По какой заполнить фактические поля?", rows)
            return
        for item in cards:
            await self.ask(item["id"], after_rate)

    async def ask(self, card_id: int, after_rate: bool = False) -> bool:
        """Спросить категорию (если поля пустые). True — спросили."""
        fields = await self.fields()
        if not fields:
            return False
        card = await self.kaiten.get_card(card_id)
        if not card:
            return False
        self._remember_board(card)
        props = card.get("properties") or {}
        if _filled(props.get(fields[0])) and _filled(props.get(fields[1])):
            if after_rate:
                self._save_state(card_id, {"after_rate": True, "done": True})
                await self.close_now(card_id)
            return False
        state = self._state(card_id)
        if state.get("asked") and not after_rate:
            return False  # уже спрашивали по «закрываем» — не дублируем
        if not self.cfg.fields_in_bot:
            # FIELDS_IN_BOT=false: поля заполняешь в Kaiten сам, бот только напоминает
            state.update({"asked": True, "after_rate": after_rate or state.get("after_rate", False),
                          "title": str(card.get("title") or card_id)})
            self._save_state(card_id, state)
            await self.alert.send(f"📝 «{state['title']}» (№{card_id}): заполни в Kaiten фактическую категорию и "
                                  "«что сделали» — без них Kaiten не закроет заявку.", self.relay._buttons(card_id))
            return True
        options = await self.options()
        guesses, solutions = self._guess(card, options)
        state.update({"asked": True, "after_rate": after_rate or state.get("after_rate", False),
                      "solutions": solutions, "title": str(card.get("title") or card_id)})
        self._save_state(card_id, state)
        title = state["title"]
        if _filled(props.get(fields[0])):
            await self._ask_solution(card_id)
            return True
        head = ("⚠️ Ты отправил оценку. Заполни фактические поля — и я закрою заявку."
                if after_rate else "📝 Закрываешь заявку — заполню фактические поля, чтобы Kaiten её закрыл.")
        rows = [[{"text": o["value"][:40], "callback_data": f"fcat:{card_id}:{o['id']}"}] for o in guesses]
        rows.append([{"text": "Другая категория…", "callback_data": f"fall:{card_id}"}])
        await self.alert.send(f"{head}\n«{title}» (№{card_id})\nКатегория проблемы?", rows)
        return True

    def _guess(self, card: dict, options: list[dict]) -> tuple[list[dict], list[str]]:
        """Вероятные категории и решения — по похожим решённым заявкам."""
        by_name = {o["value"].casefold(): o for o in options}
        found = self.relay.cases.search(card_query(card), k=8, exclude_id=card.get("id"))
        votes = Counter(c.category.strip().casefold() for c in found if c.category.strip().casefold() in by_name)
        guesses = [by_name[name] for name, _ in votes.most_common(3)]
        solutions = []
        for c in found:
            if c.solution and c.solution not in solutions:
                solutions.append(c.solution)
        return guesses, solutions[:3]

    async def show_all(self, card_id: int) -> None:
        options = await self.options()
        rows = [[{"text": o["value"][:40], "callback_data": f"fcat:{card_id}:{o['id']}"}] for o in options[:40]]
        await self.alert.send(f"Все категории для №{card_id}:", rows)

    async def set_category(self, card_id: int, option_id: int) -> bool:
        fields = await self.fields()
        option = next((o for o in await self.options() if o["id"] == option_id), None)
        if not fields or option is None:
            return False
        self._remember_board(await self.kaiten.get_card(card_id))
        value = [option_id] if await self.value_shape() == "list" else option_id
        if not self.cfg.dry_run:
            await self.kaiten.set_properties(card_id, {fields[0]: value})
        self.relay.note(card_id, "[тест] " * self.cfg.dry_run + f"записал категорию «{option['value']}»")
        state = self._state(card_id)
        state["category"] = option["value"]
        self._save_state(card_id, state)
        card = await self.kaiten.get_card(card_id)
        if card and _filled((card.get("properties") or {}).get(fields[1])) and not self.cfg.dry_run:
            await self._finish(card_id)
        else:
            await self._ask_solution(card_id)
        return True

    async def _ask_solution(self, card_id: int) -> None:
        state = self._state(card_id)
        self.storage.set("await_solution", str(card_id))
        rows = [[{"text": s[:60], "callback_data": f"fsol:{card_id}:{i}"}] for i, s in enumerate(state.get("solutions", []))]
        await self.alert.send(f"Что сделали по №{card_id}? Ответь сюда одним сообщением"
                              + (" или выбери как в похожих:" if rows else "."), rows or None)

    async def set_solution(self, card_id: int, text: str) -> bool:
        fields = await self.fields()
        text = text.strip()[:300]
        if not fields or not text:
            return False
        if not self.cfg.dry_run:
            await self.kaiten.set_properties(card_id, {fields[1]: text})
        self.relay.note(card_id, "[тест] " * self.cfg.dry_run + f"записал «что сделали»: {text[:60]}")
        if self.storage.get("await_solution") == str(card_id):
            self.storage.set("await_solution", "")
        state = self._state(card_id)
        state["solution"] = text
        self._save_state(card_id, state)
        await self._finish(card_id)
        return True

    async def pick_solution(self, card_id: int, index: int) -> bool:
        options = self._state(card_id).get("solutions", [])
        return 0 <= index < len(options) and await self.set_solution(card_id, options[index])

    async def free_text(self, text: str) -> bool:
        """Обычное сообщение тебе в личку боту: ответ на «что сделали?»."""
        waiting = self.storage.get("await_solution", "") or ""
        if not waiting.isdigit():
            return False
        try:
            return await self.set_solution(int(waiting), text)
        except KaitenError as exc:
            await self.alert.send(f"❗ Не смог записать в Kaiten: {exc}")
            return True

    async def _finish(self, card_id: int) -> None:
        state = self._state(card_id)
        state["done"] = True
        self._save_state(card_id, state)
        test = "[тест, в Kaiten не записал] " if self.cfg.dry_run else ""
        text = (f"✅ {test}№{card_id}: категория «{state.get('category') or 'уже была'}», "
                f"сделали: «{(state.get('solution') or 'уже было')[:80]}».")
        if state.get("after_rate"):
            await self.close_now(card_id, text)  # оценку уже отправил — закрываем сразу
            return
        await self.alert.send(text + " Заявку закрою, когда отправишь /оценкаработы.")

    async def close_now(self, card_id: int, note: str = "") -> bool:
        """Перенести в «Готово» и прислать кнопку «Вернуть» (наша система сама не закрывает)."""
        card = await self.kaiten.get_card(card_id)
        if not card:
            return False
        done = await self.done_column(card.get("board_id"))
        if not done:
            await self.alert.send((note + "\n" if note else "") + f"⚠️ Не нашёл колонку «Готово» — закрой №{card_id} "
                                  "вручную.", self.relay._buttons(card_id))
            return False
        from_column = card.get("column_id")
        if from_column == done:
            return True
        if not self.cfg.dry_run:
            await self.kaiten.move_card(card_id, done)
        test = "[тест, в Kaiten не закрыл] " if self.cfg.dry_run else ""
        self.relay.note(card_id, "[тест] " * self.cfg.dry_run + "закрыл после «оцените работу»")
        rows = self.relay._buttons(card_id)
        rows.append([{"text": "↩️ Вернуть", "callback_data": f"freopen:{card_id}:{from_column}"}])
        await self.alert.send((note + "\n" if note else "") +
                              f"🔒 {test}Закрыл «{card.get('title') or card_id}» (№{card_id}) — отправил оценку.", rows)
        return True

    # --- «не получили ответа — закрываем» ---
    async def _columns_with_mine(self) -> list[int]:
        await self.relay.waiting_column_ids()
        return [self.cfg.in_work_column_id, *(self.relay._waiting_ids or []), *(self.relay._recontact_ids or [])]

    async def noreply(self, keyset: set[str]) -> None:
        cards = await self._cards(keyset, await self._columns_with_mine())
        if len(cards) > 1:
            rows = [[{"text": f"Закрыть: {str(c.get('title') or c['id'])[:40]}", "callback_data": f"fnr:{c['id']}"}]
                    for c in cards[:6]]
            await self.alert.send("🔒 У этого автора несколько твоих заявок. Какую закрыть как «клиент не ответил»?",
                                  rows)
            return
        for item in cards:
            await self.close_noreply(item["id"])

    async def close_noreply(self, card_id: int) -> bool:
        fields = await self.fields()
        card = await self.kaiten.get_card(card_id)
        if not fields or not card:
            return False
        self._remember_board(card)
        props = card.get("properties") or {}
        option = None
        if not _filled(props.get(fields[0])):
            option = next((o for o in await self.options()
                           if any(w in o["value"].casefold() for w in NOREPLY_CATEGORY_WORDS)), None)
        update = {}
        if option:
            update[fields[0]] = [option["id"]] if await self.value_shape() == "list" else option["id"]
        if not _filled(props.get(fields[1])):
            update[fields[1]] = NOREPLY_SOLUTION
        if update and not self.cfg.dry_run:
            await self.kaiten.set_properties(card_id, update)
        title = str(card.get("title") or card_id)
        if not option and not _filled(props.get(fields[0])):
            # нет подходящей категории — спросим, а потом дадим кнопку «Закрыть»
            self._save_state(card_id, {"solution": NOREPLY_SOLUTION, "after_rate": True, "title": title})
            await self.ask(card_id, after_rate=True)
            return False
        done = await self.done_column(card.get("board_id"))
        if not done:
            await self.alert.send(f"⚠️ Заполнил поля №{card_id}, но не нашёл колонку «Готово» — закрой вручную.",
                                  self.relay._buttons(card_id))
            return False
        from_column = card.get("column_id")
        if not self.cfg.dry_run:
            await self.kaiten.move_card(card_id, done)
        test = "[тест, в Kaiten ничего не менял] " if self.cfg.dry_run else ""
        category = option["value"] if option else "уже была"
        self.relay.note(card_id, "[тест] " * self.cfg.dry_run + f"закрыл: клиент не ответил (категория «{category}»)")
        rows = self.relay._buttons(card_id)
        rows.append([{"text": "↩️ Вернуть", "callback_data": f"freopen:{card_id}:{from_column}"}])
        await self.alert.send(f"🔒 {test}Закрыл «{title}» (№{card_id}) — клиент не ответил.\n"
                              f"Категория: «{category}», итог: «{update.get(fields[1], 'уже был')}».", rows)
        return True

    async def reopen(self, card_id: int, column_id: int) -> str:
        card = await self.kaiten.get_card(card_id)
        done = await self.done_column((card or {}).get("board_id"))
        if not card or card.get("column_id") != done:
            return "Карточка уже не в «Готово»"
        if not self.cfg.dry_run:
            await self.kaiten.move_card(card_id, column_id)
        self.relay.note(card_id, "вернул из «Готово» по кнопке")
        return "Вернул"

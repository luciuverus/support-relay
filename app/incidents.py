"""Детектор инцидентов: смотрит всю доску поддержки, а не только твои заявки.

Для старшего смены и руководителя:
- массовый сбой: несколько заявок на одну тему (оплаты, LiveDigital, вход…) за короткое время;
- всплеск: много заявок любых тем за то же время;
- заявка коллеги висит в «Очереди» дольше 5 минут (п. 4.1 регламента);
- завал: на одном уровне в «Очереди» скопилось много заявок;
- итоги дня в конце рабочего времени.

Руководитель получает только важное: массовые сбои, всплески, завалы и итоги дня.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from statistics import median

from .config import Config
from .kaiten import KaitenClient, KaitenError
from .similar import card_query, tokens
from .logic import column_title, flatten_columns, is_responsible, is_urgent, moved_at, parse_kaiten_datetime
from .storage import Storage
from .transports import AlertBot, Clock, utcnow

log = logging.getLogger(__name__)

# Темы по ключевым словам (в заголовке, описании и полях формы). Порядок важен: берётся первая подходящая.
TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("Оплаты / Битрикс", ("оплат", "платеж", "платёж", "битрикс", "bitrix", "рассрочк", "возврат", "чек ",
                          "списан", "транзакц", "эквайр")),
    ("LiveDigital / онлайн-уроки", ("livedigital", "live digital", "лайв диджитал", "вебинар", "трансляц",
                                    "конференц", "комнат")),
    ("Вход / доступ", ("не могу войти", "не входит", "войти", "авториз", "логин", "парол", "доступ",
                       "заблокир")),
    ("Видео / звук", ("видео", "звук", "микрофон", "камер", "не слышно", "не видно")),
    ("Домашние задания / тесты", ("домашн", " дз ", "тест", "задани")),
    ("Платформа / сайт", ("платформ", "сайт", "личный кабинет", "лк ", "не грузит", "не загруж", "ошибк",
                          "белый экран", "висит", "приложени")),
]
SPIKE_KEY = "*"
# Одна проблема или разные? Поля формы «Какая проблема…» (ответ из списка) и редкие общие слова.
ANSWER_FIELD = re.compile(r"^какая (у вас )?проблема", re.I)
ANSWERS_TTL = timedelta(hours=1)
COMMON_SHARE = 0.10   # слово есть больше чем в 10% заявок за неделю — оно ничего не различает
MIN_FOR_SHARE = 30    # пока заявок мало, частоту не считаем: убираем только слова темы
PROBLEM_NAMES = ("проблемные", "проблемная", "проблемы")
BOARD_TTL = timedelta(hours=1)
KEEP_DAYS = 8
MAX_LINES = 10


TOPIC_STEMS = tuple(sorted({w.strip().replace("ё", "е")[:6] for _, words in TOPICS for w in words if w.strip()}))


def card_words(card: dict) -> set[str]:
    """Слова заявки без слов темы («оплата», «битрикс» есть у всех заявок темы и ничего не различают)."""
    return {t for t in tokens(card_query(card)) if not t.startswith(TOPIC_STEMS)}


def same_problem(a: dict, b: dict, common: set[str]) -> bool:
    """Две заявки про одно и то же: одинаковый ответ формы или ≥2 общих редких слова
    (или короткий текст одной целиком входит в другую: «не проходит» и «не проходит картой»)."""
    if a["answers"] & b["answers"]:
        return True
    wa, wb = a["words"] - common, b["words"] - common
    shared = len(wa & wb)
    return shared >= 2 or (shared >= 1 and shared == min(len(wa), len(wb)))


def common_answer(rows: list[dict]) -> str:
    """Ответ формы, который дали хотя бы двое, — чтобы в уведомлении было видно, что общего."""
    counts: dict[str, int] = {}
    for r in rows:
        for a in set(filter(None, (r.get("answers") or "").split("\n"))):
            counts[a] = counts.get(a, 0) + 1
    best = max(counts.items(), key=lambda x: (x[1], x[0]), default=("", 0))
    return best[0].split(": ", 1)[-1] if best[1] >= 2 else ""


def card_text(card: dict) -> str:
    parts = [str(card.get("title") or ""), str(card.get("description") or "")]
    props = card.get("properties")
    if isinstance(props, dict):
        parts += [v for v in props.values() if isinstance(v, str)]
    text = " " + " ".join(parts).casefold().replace("ё", "е") + " "
    return re.sub(r"\s+", " ", text)


def topic_of(card: dict, extra: str = "") -> str:
    """extra — ответы формы из списка (в карточке они хранятся номерами, а тема видна по тексту ответа)."""
    text = card_text(card) + " " + extra.casefold().replace("ё", "е") + " "
    for name, words in TOPICS:
        if any(w.replace("ё", "е") in text for w in words):
            return name
    return ""


def responsible_of(card: dict) -> tuple[int | None, str]:
    for member in card.get("members") or []:
        if isinstance(member, dict) and member.get("type") == 2:
            name = member.get("full_name") or member.get("username") or member.get("email") or ""
            return member.get("id", member.get("user_id")), str(name).strip()
    rid = card.get("responsible_id")
    return (rid if isinstance(rid, int) else None), ""


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def tickets(n: int) -> str:
    return f"{n} {_plural(n, 'заявка', 'заявки', 'заявок')}"


def minutes(n: int) -> str:
    return f"{n} {_plural(n, 'минута', 'минуты', 'минут')}"


class IncidentDetector:
    def __init__(self, cfg: Config, storage: Storage, kaiten: KaitenClient, alert: AlertBot,
                 clock: Clock = utcnow, is_paused=None, name: str = ""):
        self.cfg = cfg
        self.storage = storage
        self.kaiten = kaiten
        self.alert = alert
        self.clock = clock
        self.is_paused = is_paused  # async () -> bool: твоя /pause глушит уведомления тебе
        self.name = name or cfg.specialist_name or "тимлид"
        self.manager_sent: list[str] = []
        self._boards: dict[int, tuple[datetime, dict]] = {}
        self._column_titles: dict[int, str] = {}
        self._answer_fields: dict[str, tuple[str, dict]] | None = None  # "id_7" -> (название, {id: ответ})
        self._answer_fields_at: datetime | None = None

    # --- руководитель ---
    def manager_chat(self) -> str:
        return (self.cfg.manager_chat_id or self.storage.get("manager_chat_id", "") or "").strip()

    def set_manager(self, chat_id: str) -> None:
        self.storage.set("manager_chat_id", str(chat_id))

    async def to_manager(self, text: str, buttons: list[list[dict]] | None = None) -> bool:
        chat = self.manager_chat()
        if not chat or chat == str(self.cfg.alert_chat_id):
            return False
        self.manager_sent.append(text)
        await AlertBot(self.alert.api, chat, self.alert.dry_run).send(text, buttons)
        return True

    async def to_me(self, text: str, buttons: list[list[dict]] | None = None) -> bool:
        if self.is_paused and await self.is_paused():
            return False
        await self.alert.send(text, buttons)
        return True

    # --- вспомогательное ---
    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.cfg.tz)

    def _hm(self, dt: datetime) -> str:
        return f"{self._local(dt):%H:%M}"

    def _in_work_hours(self, at: datetime) -> bool:
        local = self._local(at)
        return (local.isoweekday() in self.cfg.work_days
                and self.cfg.work_start <= local.time() < self.cfg.work_end)

    def _url(self, card_id: int) -> str:
        return f"{self.cfg.card_url_prefix}{card_id}"

    def _open_buttons(self, card_ids: list[int]) -> list[list[dict]]:
        return [[{"text": f"Открыть {cid}", "url": self._url(cid)} for cid in card_ids[:3]]] if card_ids else []

    async def _board(self, board_id: int | None) -> dict:
        if not board_id:
            return {}
        now = self.clock()
        cached = self._boards.get(board_id)
        if cached and now - cached[0] < BOARD_TTL:
            return cached[1]
        try:
            board = await self.kaiten.get_board(board_id) or {}
        except KaitenError as exc:
            log.warning("Детектор: не смог прочитать доску %s: %s", board_id, exc)
            board = cached[1] if cached else {}
        self._boards[board_id] = (now, board)
        for col in flatten_columns(board):
            if isinstance(col.get("id"), int):
                self._column_titles[col["id"]] = column_title(col)
        return board

    async def _lane(self, card: dict) -> str:
        """Уровень поддержки: дорожка на доске, иначе название колонки."""
        board = await self._board(card.get("board_id"))
        lane_id = card.get("lane_id")
        for lane in board.get("lanes") or []:
            if isinstance(lane, dict) and lane.get("id") == lane_id and lane_id is not None:
                return str(lane.get("title") or lane.get("name") or "").strip()
        lane = card.get("lane")
        if isinstance(lane, dict) and lane.get("title"):
            return str(lane["title"]).strip()
        return self._column_titles.get(card.get("column_id"), "") or "Очередь"

    async def _answers_map(self) -> dict[str, tuple[str, dict]]:
        """Поля формы «Какая проблема…» и их варианты (раз в час). Не прочиталось — работаем по тексту."""
        now = self.clock()
        if self._answer_fields is not None and self._answer_fields_at and now - self._answer_fields_at < ANSWERS_TTL:
            return self._answer_fields
        fields: dict[str, tuple[str, dict]] = {}
        try:
            for prop in await self.kaiten.list_custom_properties():
                name = str(prop.get("name") or "").strip()
                if not isinstance(prop.get("id"), int) or not ANSWER_FIELD.match(name):
                    continue
                values = {}
                if prop.get("type") in ("select", "multi_select"):
                    values = {v["id"]: str(v.get("value") or "") for v in await self.kaiten.list_select_values(prop["id"])
                              if isinstance(v, dict) and isinstance(v.get("id"), int)}
                fields[f"id_{prop['id']}"] = (name, values)
        except KaitenError as exc:
            log.warning("Детектор: поля формы не прочитались, сбои — только по тексту: %s", exc)
            fields = self._answer_fields or {}
        self._answer_fields, self._answer_fields_at = fields, now
        return fields

    async def card_answers(self, card: dict) -> set[str]:
        """Ответы на «Какая проблема…»: «Какая у вас проблема со счетом?: Клиент не может оплатить»."""
        props = card.get("properties")
        if not isinstance(props, dict):
            return set()
        out = set()
        for key, (name, values) in (await self._answers_map()).items():
            raw = props.get(key)
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                label = values.get(item, "") if isinstance(item, int) else str(item or "")
                label = re.sub(r"\s+", " ", label).strip()
                if label and not label.casefold().startswith("другое"):
                    out.add(f"{name}: {label}")
        return out

    # --- главный цикл ---
    async def poll(self) -> list[str]:
        """Раз в минуту: обновить картину доски и прислать то, что требует внимания."""
        if not self.cfg.incidents:
            return []
        now = self.clock()
        queue: list[dict] = []
        complete = True
        for column_id in sorted(self.cfg.queue_column_ids):
            try:
                queue += await self.kaiten.list_cards(column_id)
            except KaitenError as exc:
                complete = False
                log.warning("Детектор: колонка %s не прочиталась: %s", column_id, exc)
        try:
            in_work = await self.kaiten.list_cards(self.cfg.in_work_column_id)
        except KaitenError as exc:
            in_work = []
            log.warning("Детектор: «В работе» не прочиталась: %s", exc)
        in_queue_ids: set[int] = set()
        for item in queue:
            if isinstance(item.get("id"), int):
                in_queue_ids.add(item["id"])
                await self._remember(item, now, in_queue=True)
        window_start = now - timedelta(minutes=self.cfg.incident_window_min)
        for item in in_work:
            # взяли быстрее, чем мы успели увидеть в «Очереди», — для массового сбоя всё равно считаем
            created = parse_kaiten_datetime(item.get("created"))
            if isinstance(item.get("id"), int) and created and created >= window_start:
                await self._remember(item, now, in_queue=False)
        if complete:
            self.storage.team_left_queue(in_queue_ids, now)
        self.storage.team_prune(now - timedelta(days=KEEP_DAYS))
        results: list[str] = []
        results += await self._check_mass(now)
        results += await self._check_untaken(now)
        results += await self._check_backlog(now)
        return results

    async def _remember(self, item: dict, now: datetime, in_queue: bool) -> None:
        created = parse_kaiten_datetime(item.get("created")) or now
        entered = moved_at(item) or created
        rid, rname = responsible_of(item)
        answers = await self.card_answers(item)
        self.storage.team_seen(
            item["id"], title=str(item.get("title") or f"Заявка {item['id']}"), lane=await self._lane(item),
            topic=topic_of(item, " ".join(answers)), created_at=min(created, now), entered_at=min(max(entered, created), now), now=now,
            responsible_id=rid, responsible=rname, in_queue=in_queue, urgent=is_urgent(item),
            words=" ".join(sorted(card_words(item))), answers="\n".join(sorted(answers)))

    # --- массовый сбой и всплеск ---
    async def _check_mass(self, now: datetime) -> list[str]:
        window = timedelta(minutes=self.cfg.incident_window_min)
        recent = self.storage.team_created_since(now - window)
        groups: dict[str, list[dict]] = {}
        for row in recent:
            if row["topic"]:
                groups.setdefault(row["topic"], []).append(row)
        results = []
        common = self._common_words(now) if self.cfg.incident_by_text else set()
        for topic, rows in sorted(groups.items()):
            if len(rows) < self.cfg.incident_same_topic:
                continue
            if self.cfg.incident_by_text:
                rows = self._largest_same_problem(rows, common)
                if len(rows) < self.cfg.incident_same_topic:
                    continue
            results += await self._episode(topic, rows, now)
        if len(recent) >= self.cfg.incident_spike:
            results += await self._episode(SPIKE_KEY, recent, now)
        return results

    def _common_words(self, now: datetime) -> set[str]:
        """Слова, которые встречаются больше чем в 10% заявок за неделю, — фон, а не признак проблемы."""
        week = self.storage.team_created_since(now - timedelta(days=7))
        if len(week) < MIN_FOR_SHARE:
            return set()
        df: dict[str, int] = {}
        for row in week:
            for w in set((row.get("words") or "").split()):
                df[w] = df.get(w, 0) + 1
        return {w for w, n in df.items() if n / len(week) > COMMON_SHARE}

    @staticmethod
    def _largest_same_problem(rows: list[dict], common: set[str]) -> list[dict]:
        """Самая большая группа «одна проблема»: заявка и все, что на неё похожи (без цепочек A~B~C)."""
        items = [{"row": r, "words": set((r.get("words") or "").split()),
                  "answers": set(filter(None, (r.get("answers") or "").split("\n")))} for r in rows]
        best: list[dict] = []
        for seed in items:
            group = [x for x in items if x is seed or same_problem(seed, x, common)]
            if len(group) > len(best):
                best = group
        return [x["row"] for x in best]

    async def _episode(self, key: str, rows: list[dict], now: datetime) -> list[str]:
        """Один сбой = одно уведомление; повтор только когда заявок стало вдвое больше."""
        state_key = f"incident:{key}"
        raw = self.storage.get(state_key, "")
        state = json.loads(raw) if raw else {}
        window = timedelta(minutes=self.cfg.incident_window_min)
        active = state and now - datetime.fromisoformat(state["updated"]) < window
        count = len(rows)
        if active:
            state["updated"] = now.isoformat()
            if count < 2 * state["notified"]:
                self.storage.set(state_key, json.dumps(state))
                return []
            kind = "update"
        else:
            state = {"started": now.isoformat(), "updated": now.isoformat(), "notified": 0}
            kind = "new"
        state["notified"] = count
        self.storage.set(state_key, json.dumps(state))
        self._log_incident(key, count, now, kind)
        rows = sorted(rows, key=lambda r: r["created_at"])
        first = rows[0]["created_at"]
        by_lane: dict[str, int] = {}
        for r in rows:
            by_lane[r["lane"] or "—"] = by_lane.get(r["lane"] or "—", 0) + 1
        lanes = ", ".join(f"{k} — {v}" for k, v in sorted(by_lane.items(), key=lambda x: -x[1]))
        lines = [f"• {r['card_id']} «{r['title'][:70]}» ({r['lane'] or '—'})" for r in rows[-MAX_LINES:]]
        span = self.cfg.incident_window_min
        if key == SPIKE_KEY:
            head = (f"📈 Всплеск: {tickets(count)} за {minutes(span)}" if kind == "new"
                    else f"📈 Всплеск продолжается: уже {tickets(count)}")
            mgr = (f"Техподдержка: всплеск обращений — {tickets(count)} за {minutes(span)} (с {self._hm(first)}).\n"
                   f"По уровням: {lanes}.")
        else:
            similar = " похожих" if self.cfg.incident_by_text else ""
            head = (f"🚨 Похоже на массовый сбой: «{key}» — {tickets(count)}{similar} за {minutes(span)}" if kind == "new"
                    else f"🚨 Сбой «{key}» растёт: уже {tickets(count)}{similar}")
            mgr = (f"Техподдержка: возможный массовый сбой — «{key}», {tickets(count)}{similar} за {minutes(span)} "
                   f"(первая в {self._hm(first)}).\nПо уровням: {lanes}.")
            answer = common_answer(rows)
            if answer:
                head += f"\nОбщее: «{answer}»"
                mgr += f"\nОбщее: «{answer}»."

        me = (f"{head}\nПо уровням: {lanes}\n" + "\n".join(lines) +
              ("\nПроверь общую причину и предупреди первую линию." if kind == "new" else ""))
        mgr += f"\nТимлид ({self.name}) в курсе и разбирается." if kind == "new" else ""
        ids = [r["card_id"] for r in rows[-3:]]
        await self.to_me(me, self._open_buttons(ids))
        await self.to_manager(mgr)
        return [f"incident:{key}:{kind}:{count}"]

    def _log_incident(self, key: str, count: int, now: datetime, kind: str) -> None:
        raw = self.storage.get("incident_log", "")
        entries = json.loads(raw) if raw else []
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
        entries = [e for e in entries if e["at"] >= cutoff]
        if kind == "new":
            entries.append({"at": now.isoformat(), "key": key, "count": count})
        elif entries:
            for e in reversed(entries):
                if e["key"] == key:
                    e["count"] = count
                    break
        self.storage.set("incident_log", json.dumps(entries, ensure_ascii=False))

    def incidents_since(self, since: datetime) -> list[dict]:
        raw = self.storage.get("incident_log", "")
        return [e for e in (json.loads(raw) if raw else []) if datetime.fromisoformat(e["at"]) >= since]

    # --- заявки коллег без ответа ---
    def _is_mine(self, row: dict) -> bool:
        return row["responsible_id"] == self.cfg.my_user_id

    async def _check_untaken(self, now: datetime) -> list[str]:
        results: list[str] = []
        limit = self.cfg.untaken_alert_min
        if limit > 0 and self._in_work_hours(now):
            late = [r for r in self.storage.team_queue()
                    if not r["untaken_alerted"] and not self._is_mine(r)
                    and now - r["entered_at"] >= timedelta(minutes=limit)]
            if late:
                late.sort(key=lambda r: (not r["urgent"], r["entered_at"]))
                if await self.to_me(self._untaken_text(late, now, limit), self._open_buttons(
                        [r["card_id"] for r in late])):
                    for r in late:
                        self.storage.team_mark(r["card_id"], untaken_alerted=1)
                    results.append(f"untaken:{len(late)}")
        esc = self.cfg.untaken_manager_min
        if esc > 0 and self.manager_chat():
            old = [r for r in self.storage.team_queue()
                   if not r["escalated"] and now - r["entered_at"] >= timedelta(minutes=esc)]
            if old:
                old.sort(key=lambda r: r["entered_at"])
                text = "Техподдержка: заявки без ответа дольше " + minutes(esc) + ":\n" + "\n".join(
                    f"• {r['card_id']} «{r['title'][:60]}» — {r['lane'] or '—'}, "
                    f"{int((now - r['entered_at']).total_seconds() // 60)} мин" for r in old[:MAX_LINES])
                await self.to_manager(text, self._open_buttons([r["card_id"] for r in old]))
                for r in old:
                    self.storage.team_mark(r["card_id"], escalated=1)
                results.append(f"escalated:{len(old)}")
        return results

    def _untaken_text(self, rows: list[dict], now: datetime, limit: int) -> str:
        def line(r: dict) -> str:
            waited = int((now - r["entered_at"]).total_seconds() // 60)
            who = r["responsible"] or "никто не назначен"
            fire = "🔥 " if r["urgent"] else ""
            return f"{fire}{r['card_id']} «{r['title'][:60]}» — {r['lane'] or '—'}, {who}, {waited} мин"
        if len(rows) == 1:
            return f"⏳ Заявка ждёт в «Очереди» дольше {minutes(limit)} (п. 4.1):\n{line(rows[0])}"
        more = f"\n…и ещё {len(rows) - MAX_LINES}" if len(rows) > MAX_LINES else ""
        return (f"⏳ {tickets(len(rows))} ждут в «Очереди» дольше {minutes(limit)} (п. 4.1):\n"
                + "\n".join(line(r) for r in rows[:MAX_LINES]) + more)

    # --- завал на уровне ---
    async def _check_backlog(self, now: datetime) -> list[str]:
        limit = self.cfg.backlog_limit
        if limit <= 0:
            return []
        counts: dict[str, int] = {}
        for r in self.storage.team_queue():
            counts[r["lane"] or "Очередь"] = counts.get(r["lane"] or "Очередь", 0) + 1
        results = []
        for lane in sorted(set(counts) | {k[len("backlog:"):] for k in self._active_backlogs()}):
            n = counts.get(lane, 0)
            key = f"backlog:{lane}"
            active = self.storage.get(key, "") == "1"
            if n >= limit and not active:
                self.storage.set(key, "1")
                self._log_incident(f"Завал: {lane}", n, now, "new")
                await self.to_me(f"📥 Завал на уровне «{lane}»: в «Очереди» {tickets(n)}. "
                                 f"Нужна помощь с разбором?")
                await self.to_manager(f"Техподдержка: на уровне «{lane}» в очереди {tickets(n)} "
                                      f"(порог {limit}). Тимлид ({self.name}) в курсе.")
                results.append(f"backlog:{lane}:{n}")
            elif active and n <= max(limit - 2, 0):
                self.storage.set(key, "")
        return results

    def _active_backlogs(self) -> list[str]:
        rows = self.storage._exec("SELECT key FROM kv WHERE key LIKE 'backlog:%' AND value = '1'")
        return [r["key"] for r in rows]

    # --- сводки ---
    def snapshot(self) -> str:
        """/team — что сейчас в очереди у всей команды."""
        now = self.clock()
        queue = sorted(self.storage.team_queue(), key=lambda r: r["entered_at"])
        if not self.cfg.incidents:
            return "Детектор выключен (INCIDENTS=false в .env)."
        lines = [f"👥 Команда сейчас ({self._hm(now)})"]
        if not queue:
            lines.append("В «Очереди» пусто.")
        else:
            by_lane: dict[str, list[dict]] = {}
            for r in queue:
                by_lane.setdefault(r["lane"] or "Очередь", []).append(r)
            for lane, rows in sorted(by_lane.items()):
                oldest = int((now - rows[0]["entered_at"]).total_seconds() // 60)
                lines.append(f"• {lane}: {tickets(len(rows))} в очереди, самая старая ждёт {oldest} мин")
        active = [e for e in self.incidents_since(now - timedelta(minutes=self.cfg.incident_window_min * 2))]
        if active:
            lines.append("Сбои за последний час: " + "; ".join(
                f"{'всплеск' if e['key'] == SPIKE_KEY else e['key']} ({tickets(e['count'])})" for e in active))
        chat = self.manager_chat()
        lines.append("Руководитель получает сводки: " + ("да" if chat else "нет (пусть напишет боту /start)"))
        return "\n".join(lines)

    def digest_text(self, since: datetime, until: datetime) -> str:
        created = [r for r in self.storage.team_created_since(since) if r["created_at"] <= until]
        left = [r for r in self.storage.team_left_since(since) if r["left_queue_at"] <= until]
        waits = [(r, (r["left_queue_at"] - r["entered_at"]).total_seconds() / 60) for r in left]
        waits = [(r, max(w, 0.0)) for r, w in waits]
        on_time = [w for _, w in waits if w <= 5]
        lines = [f"📊 Итоги дня {self._local(since):%d.%m} (с {self._hm(since)} до {self._hm(until)})"]
        if not created and not waits:
            lines.append("Новых заявок не было (или сервис был выключен).")
            return "\n".join(lines)
        by_lane: dict[str, int] = {}
        for r in created:
            by_lane[r["lane"] or "—"] = by_lane.get(r["lane"] or "—", 0) + 1
        lines.append(f"Новых заявок: {len(created)}" + (
            " — " + ", ".join(f"{k} {v}" for k, v in sorted(by_lane.items(), key=lambda x: -x[1])) if by_lane else ""))
        if waits:
            share = round(100 * len(on_time) / len(waits))
            lines.append(f"Взято за 5 минут: {share}% ({len(on_time)} из {len(waits)}), "
                         f"медиана ожидания {median(w for _, w in waits):.0f} мин")
            slow_row, slow = max(waits, key=lambda x: x[1])
            if slow > 5:
                lines.append(f"Дольше всех ждала: {slow_row['card_id']} «{slow_row['title'][:60]}» — "
                             f"{slow:.0f} мин ({slow_row['lane'] or '—'})")
        still = self.storage.team_queue()
        if still:
            lines.append(f"Сейчас в очереди: {tickets(len(still))}")
        topics: dict[str, int] = {}
        for r in created:
            if r["topic"]:
                topics[r["topic"]] = topics.get(r["topic"], 0) + 1
        if topics:
            top = sorted(topics.items(), key=lambda x: -x[1])[:3]
            lines.append("Темы: " + ", ".join(f"{k} {v}" for k, v in top))
        incidents = [e for e in self.incidents_since(since) if datetime.fromisoformat(e["at"]) <= until]
        if incidents:
            lines.append("Инциденты: " + "; ".join(
                f"{self._hm(datetime.fromisoformat(e['at']))} "
                f"{'всплеск' if e['key'] == SPIKE_KEY else e['key']} ({tickets(e['count'])})" for e in incidents))
        else:
            lines.append("Массовых сбоев не было.")
        if self.cfg.digest_by_person and waits:
            people: dict[str, list[float]] = {}
            for r, w in waits:
                people.setdefault(r["responsible"] or "без ответственного", []).append(w)
            lines.append("По специалистам (взято / медиана ожидания):")
            for who, ws in sorted(people.items(), key=lambda x: -len(x[1])):
                late = sum(1 for w in ws if w > 5)
                lines.append(f"• {who}: {len(ws)} / {median(ws):.0f} мин" + (f", позже 5 мин — {late}" if late else ""))
        lines.append("Данные — за время, пока работал сервис.")
        from .reports import team_source_note
        lines.append(team_source_note(self.cfg))
        return "\n".join(lines)

    def _day_start(self, now: datetime) -> datetime:
        local = self._local(now)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    async def digest(self) -> bool:
        """Раз в день в конце рабочего времени: итоги тебе и руководителю."""
        if not (self.cfg.incidents and self.cfg.digest):
            return False
        now = self.clock()
        local = self._local(now)
        today = local.date().isoformat()
        if local.time() < self.cfg.work_end or self.storage.get("digest_date") == today:
            return False
        self.storage.set("digest_date", today)
        text = self.digest_text(self._day_start(now), now)
        await self.to_me(text)
        await self.to_manager(text)
        return True

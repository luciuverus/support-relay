"""Состояние в SQLite: переживает перезапуск (взятые карточки, очередь сообщений, SLA)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    warn_at TEXT NOT NULL,
    warn_sent INTEGER NOT NULL DEFAULT 0,
    breach_sent INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    contact TEXT NOT NULL,
    text TEXT NOT NULL,
    send_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT,
    sent_at TEXT,
    series TEXT
);
CREATE TABLE IF NOT EXISTS chats (
    key TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    last_in_at TEXT,
    last_msg_id INTEGER,
    name TEXT,
    last_out_at TEXT
);
CREATE TABLE IF NOT EXISTS waiting (
    card_id INTEGER PRIMARY KEY,
    since TEXT NOT NULL,
    delay_s REAL NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS team_cards (
    card_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    responsible_id INTEGER,
    responsible TEXT NOT NULL DEFAULT '',
    in_queue INTEGER NOT NULL DEFAULT 1,
    left_queue_at TEXT,
    untaken_alerted INTEGER NOT NULL DEFAULT 0,
    escalated INTEGER NOT NULL DEFAULT 0,
    urgent INTEGER NOT NULL DEFAULT 0,
    words TEXT,
    answers TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    card_id INTEGER,
    action TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_card ON journal(card_id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # база от прошлых версий: добавляем недостающие колонки
            for table, column in (("chats", "name"), ("chats", "last_out_at"),
                                  ("outbox", "created_at"), ("outbox", "sent_at"),
                                  ("outbox", "series"), ("team_cards", "words"), ("team_cards", "answers")):
                columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    # --- карточки ---
    def claim_card(self, card_id: int, title: str, url: str, taken_at: datetime,
                   deadline: datetime, warn_at: datetime) -> bool:
        """True, если карточку взяли впервые. Атомарно — защищает от дублей вебхуков."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO cards(card_id, title, url, taken_at, deadline, warn_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (card_id, title, url, _iso(taken_at), _iso(deadline), _iso(warn_at)),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def release_card(self, card_id: int) -> None:
        """Откат claim, если взять в работу в Kaiten не удалось — следующий вебхук попробует снова."""
        self._exec("DELETE FROM cards WHERE card_id = ?", (card_id,))

    def has_card(self, card_id: int) -> bool:
        return bool(self._exec("SELECT 1 FROM cards WHERE card_id = ?", (card_id,)))

    def open_cards(self) -> list[dict]:
        rows = self._exec("SELECT * FROM cards WHERE closed = 0")
        return [
            {**dict(r), "deadline": _dt(r["deadline"]), "warn_at": _dt(r["warn_at"]),
             "taken_at": _dt(r["taken_at"])}
            for r in rows
        ]

    def set_deadline(self, card_id: int, deadline: datetime, warn_at: datetime) -> None:
        """Новый срок SLA (заявку взяли в работу после оценки) — напоминания начинаются заново."""
        self._exec("UPDATE cards SET deadline = ?, warn_at = ?, warn_sent = 0, breach_sent = 0, closed = 0 "
                   "WHERE card_id = ?", (_iso(deadline), _iso(warn_at), card_id))

    def mark_card(self, card_id: int, **flags: int) -> None:
        allowed = {"warn_sent", "breach_sent", "closed"}
        for name, value in flags.items():
            if name not in allowed:
                raise ValueError(name)
            self._exec(f"UPDATE cards SET {name} = ? WHERE card_id = ?", (int(value), card_id))

    # --- очередь сообщений ---
    def enqueue(self, card_id: int, role: str, contact: str, text: str, send_at: datetime,
                created_at: datetime | None = None, series: str | None = None) -> int:
        """series — одна «серия» сообщений (приветствие по одному взятию, одно напоминание).

        Все проверки «уже начали писать?» смотрят только на свою серию, а не на всю историю заявки."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbox(card_id, role, contact, text, send_at, created_at, series) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (card_id, role, contact, text, _iso(send_at), _iso(created_at or send_at), series),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def due_messages(self, now: datetime) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM outbox WHERE status = 'pending' AND send_at <= ? ORDER BY send_at, id",
            (_iso(now),),
        )
        return [dict(r) for r in rows]

    def all_messages(self) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM outbox ORDER BY id")]

    def set_message_status(self, msg_id: int, status: str, error: str | None = None,
                           at: datetime | None = None) -> None:
        if status == "sent":
            self._exec("UPDATE outbox SET status = ?, error = ?, sent_at = ? WHERE id = ?",
                       (status, error, _iso(at or datetime.now(timezone.utc)), msg_id))
        else:
            self._exec("UPDATE outbox SET status = ?, error = ? WHERE id = ?", (status, error, msg_id))

    def pending_group(self, card_id: int, role: str, statuses: tuple[str, ...] = ("pending",)) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self._exec(f"SELECT * FROM outbox WHERE status IN ({marks}) AND card_id = ? AND role = ? "
                          "ORDER BY send_at, id", (*statuses, card_id, role))
        return [dict(r) for r in rows]

    def message_status(self, msg_id: int) -> str | None:
        rows = self._exec("SELECT status FROM outbox WHERE id = ?", (msg_id,))
        return rows[0]["status"] if rows else None

    def current_series(self, card_id: int, role: str) -> str | None:
        """Серия, которая сейчас ждёт отправки (pending/held) по этой заявке и адресату."""
        rows = self._exec("SELECT COALESCE(series, '') AS s FROM outbox WHERE status IN ('pending', 'held') "
                          "AND card_id = ? AND role = ? ORDER BY send_at, id LIMIT 1", (card_id, role))
        return rows[0]["s"] if rows else None

    def group_started(self, card_id: int, role: str, series: str | None = None) -> bool:
        """Ушёл ли уже хоть один «пузырь» ТЕКУЩЕЙ серии (а не когда-то раньше по этой заявке)."""
        if series is None:
            series = self.current_series(card_id, role)
        if series is None:
            return False  # ждать нечего — и начинать нечего
        return bool(self._exec("SELECT 1 FROM outbox WHERE status = 'sent' AND card_id = ? AND role = ? "
                               "AND COALESCE(series, '') = ?", (card_id, role, series)))

    def fail_group(self, card_id: int, role: str, error: str, status: str = "failed",
                   statuses: tuple[str, ...] = ("pending",)) -> list[str]:
        """Помечает неотправленные «пузыри» группы (failed/cancelled/held/...) и возвращает их тексты."""
        rows = self.pending_group(card_id, role, statuses)
        for row in rows:
            self.set_message_status(row["id"], status, error)
        return [row["text"] for row in rows]

    def groups_with_status(self, status: str) -> list[dict]:
        """Группы (card_id, role, contact) с сообщениями в данном статусе и временем создания."""
        rows = self._exec("SELECT card_id, role, contact, MIN(created_at) AS created_at FROM outbox "
                          "WHERE status = ? GROUP BY card_id, role, contact", (status,))
        return [{**dict(r), "created_at": _dt(r["created_at"])} for r in rows]

    def release_group(self, card_id: int, role: str, start: datetime, gap_s: float) -> int:
        rows = self.pending_group(card_id, role, ("held",))
        for position, row in enumerate(rows):
            self._exec("UPDATE outbox SET status = 'pending', error = NULL, send_at = ? WHERE id = ?",
                       (_iso(start + timedelta(seconds=position * gap_s)), row["id"]))
        return len(rows)

    def last_sent_to(self, contact: str) -> datetime | None:
        rows = self._exec("SELECT MAX(sent_at) AS t FROM outbox WHERE status = 'sent' AND contact = ?", (contact,))
        return _dt(rows[0]["t"]) if rows and rows[0]["t"] else None

    def stats_since(self, since: datetime) -> dict:
        taken = self._exec("SELECT COUNT(*) AS n FROM cards WHERE taken_at >= ?", (_iso(since),))[0]["n"]
        rows = self._exec("SELECT status, COUNT(DISTINCT card_id || ':' || role) AS n FROM outbox "
                          "WHERE created_at >= ? GROUP BY status", (_iso(since),))
        by_status = {r["status"]: r["n"] for r in rows}
        followups = self._exec("SELECT COUNT(DISTINCT card_id) AS n FROM outbox WHERE role = 'followup' "
                               "AND status = 'sent' AND sent_at >= ?", (_iso(since),))[0]["n"]
        return {"taken": taken, "by_status": by_status, "followups": followups}

    # --- ожидание ответа ---
    def waiting_row(self, card_id: int) -> dict | None:
        rows = self._exec("SELECT * FROM waiting WHERE card_id = ?", (card_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["since"] = _dt(row["since"])
        return row

    def set_waiting(self, card_id: int, since: datetime, delay_s: float, done: bool) -> None:
        self._exec("INSERT INTO waiting(card_id, since, delay_s, done) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(card_id) DO UPDATE SET since = excluded.since, delay_s = excluded.delay_s, "
                   "done = excluded.done", (card_id, _iso(since), delay_s, int(done)))

    def mark_waiting_done(self, card_id: int) -> None:
        self._exec("UPDATE waiting SET done = 1 WHERE card_id = ?", (card_id,))

    def forget_waiting_except(self, card_ids: set[int]) -> None:
        for row in self._exec("SELECT card_id FROM waiting"):
            if row["card_id"] not in card_ids:
                self._exec("DELETE FROM waiting WHERE card_id = ?", (row["card_id"],))

    def wipe_test_state(self) -> None:
        """Для перехода из тестового режима: забыть взятые карточки и очередь, но НЕ чаты."""
        for table in ("cards", "outbox", "waiting"):
            self._exec(f"DELETE FROM {table}")
        self._exec("DELETE FROM kv WHERE key LIKE 'last_variant:%' OR key LIKE 'greeted:%' OR key LIKE 'assess:%'")

    # --- вся доска (детектор инцидентов) ---
    _TEAM_TIMES = ("created_at", "entered_at", "first_seen", "left_queue_at")

    def _team_row(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        for key in self._TEAM_TIMES:
            data[key] = _dt(data[key])
        return data

    def team_card(self, card_id: int) -> dict | None:
        rows = self._exec("SELECT * FROM team_cards WHERE card_id = ?", (card_id,))
        return self._team_row(rows[0]) if rows else None

    def team_seen(self, card_id: int, *, title: str, lane: str, topic: str, created_at: datetime,
                  entered_at: datetime, now: datetime, responsible_id: int | None, responsible: str,
                  in_queue: bool, urgent: bool, words: str = "", answers: str = "") -> bool:
        """Запомнить карточку доски. True — видим впервые.

        words — отличительные слова текста, answers — ответы формы «Какая проблема…» (по строке):
        по ним массовый сбой отличает одну проблему от разных проблем одной темы."""
        known = self.team_card(card_id)
        if known is None:
            self._exec("INSERT INTO team_cards (card_id, title, lane, topic, created_at, entered_at, first_seen, "
                       "responsible_id, responsible, in_queue, left_queue_at, urgent, words, answers) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (card_id, title, lane, topic, _iso(created_at), _iso(entered_at), _iso(now),
                        responsible_id, responsible, int(in_queue), None if in_queue else _iso(now), int(urgent),
                        words, answers))
            return True
        if (words and not known.get("words")) or (answers and not known.get("answers")):
            self._exec("UPDATE team_cards SET words = ?, answers = ? WHERE card_id = ?",
                       (words or known.get("words") or "", answers or known.get("answers") or "", card_id))
        if in_queue and not known["in_queue"]:
            # карточку вернули в «Очередь» — новый заход, считаем ожидание заново
            self._exec("UPDATE team_cards SET in_queue = 1, left_queue_at = NULL, entered_at = ?, "
                       "untaken_alerted = 0, escalated = 0 WHERE card_id = ?", (_iso(entered_at), card_id))
        if in_queue:
            self._exec("UPDATE team_cards SET title = ?, lane = ?, responsible_id = ?, responsible = ?, urgent = ? "
                       "WHERE card_id = ?", (title, lane, responsible_id, responsible, int(urgent), card_id))
        return False

    def team_left_queue(self, still_in_queue: set[int], at: datetime) -> list[int]:
        """Карточки, которых больше нет в «Очереди»: их взяли (или перенесли) — запоминаем когда."""
        gone = [r["card_id"] for r in self._exec("SELECT card_id FROM team_cards WHERE in_queue = 1")
                if r["card_id"] not in still_in_queue]
        for card_id in gone:
            self._exec("UPDATE team_cards SET in_queue = 0, left_queue_at = ? WHERE card_id = ?", (_iso(at), card_id))
        return gone

    def team_mark(self, card_id: int, **flags: int) -> None:
        for key, value in flags.items():
            if key not in {"untaken_alerted", "escalated"}:
                raise ValueError(key)
            self._exec(f"UPDATE team_cards SET {key} = ? WHERE card_id = ?", (value, card_id))

    def team_queue(self) -> list[dict]:
        return [self._team_row(r) for r in self._exec("SELECT * FROM team_cards WHERE in_queue = 1")]

    def team_created_since(self, since: datetime) -> list[dict]:
        return [self._team_row(r) for r in self._exec("SELECT * FROM team_cards WHERE created_at >= ?",
                                                       (_iso(since),))]

    def team_left_since(self, since: datetime) -> list[dict]:
        return [self._team_row(r) for r in self._exec("SELECT * FROM team_cards WHERE left_queue_at >= ?",
                                                       (_iso(since),))]

    def team_prune(self, before: datetime) -> None:
        self._exec("DELETE FROM team_cards WHERE in_queue = 0 AND first_seen < ?", (_iso(before),))

    # --- журнал действий (/log) ---
    def journal_add(self, at: datetime, card_id: int | None, action: str) -> None:
        self._exec("INSERT INTO journal(at, card_id, action) VALUES (?, ?, ?)", (_iso(at), card_id, action))

    def journal(self, card_id: int | None = None, limit: int = 20) -> list[dict]:
        """Последние записи (или вся история одной заявки) — от старых к новым."""
        if card_id is None:
            rows = self._exec("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._exec("SELECT * FROM journal WHERE card_id = ? ORDER BY id DESC LIMIT ?", (card_id, limit))
        return [{**dict(r), "at": _dt(r["at"])} for r in reversed(rows)]

    def journal_between(self, since: datetime, until: datetime) -> list[dict]:
        rows = self._exec("SELECT * FROM journal WHERE at >= ? AND at < ? ORDER BY id", (_iso(since), _iso(until)))
        return [{**dict(r), "at": _dt(r["at"])} for r in rows]

    def journal_prune(self, before: datetime) -> None:
        self._exec("DELETE FROM journal WHERE at < ?", (_iso(before),))

    # --- срок хранения (меньше данных о людях на компьютере) ---
    CARD_KEYS = ("author_key:{id}", "client_key:{id}", "fill:{id}", "offshift_alerted:{id}", "pause_alerted:{id}",
                 "waithint:{id}", "no_auto:{id}", "assess:{id}")

    def prune_old(self, messages_before: datetime, data_before: datetime,
                  marks_before: datetime | None = None) -> dict[str, int]:
        """Удалить отправленные тексты старше messages_before, а чаты, заявки и отметки по ним — старше data_before."""
        counts = {}
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM outbox WHERE status NOT IN ('pending', 'held') "
                "AND COALESCE(sent_at, created_at, send_at) < ?", (_iso(messages_before),))
            counts["messages"] = cur.rowcount
            cur = self._conn.execute(
                "DELETE FROM chats WHERE COALESCE(last_in_at, last_out_at) IS NOT NULL "
                "AND MAX(COALESCE(last_in_at, ''), COALESCE(last_out_at, '')) < ?", (_iso(data_before),))
            counts["chats"] = cur.rowcount
            old = [r["card_id"] for r in self._conn.execute(
                "SELECT card_id FROM cards WHERE taken_at < ?", (_iso(data_before),))]
            for card_id in old:
                self._conn.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))
                for pattern in self.CARD_KEYS:
                    self._conn.execute("DELETE FROM kv WHERE key = ?", (pattern.format(id=card_id),))
                for prefix in (f"greeted:{card_id}:", f"corpmsg:{card_id}:"):
                    self._conn.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
            counts["cards"] = len(old)
            counts["marks"] = self._prune_marks(marks_before or data_before)
            self._conn.commit()
        return counts

    def _team_card_ids(self) -> set[int]:
        return {r["card_id"] for r in self._conn.execute("SELECT card_id FROM team_cards")}

    def _prune_marks(self, day_ago: datetime) -> int:
        """Служебные отметки с контактами, которые больше не нужны (вызывается под замком).

        «Кто написал» и вопрос с текстом сообщения — старше day_ago; «какой вариант текста был последним»
        — для чатов, которых уже нет; кеш контактов и состояние полей — для заявок, которых уже нет."""
        rows = self._conn.execute("SELECT key, value FROM kv WHERE value != '' AND (key LIKE 'whoq:%' OR "
                                  "key LIKE 'who:%' OR key LIKE 'last_variant:%:%' OR key LIKE 'author_key:%' OR "
                                  "key LIKE 'client_key:%' OR key LIKE 'fill:%')").fetchall()
        chats = {r["key"] for r in self._conn.execute("SELECT key FROM chats")}
        cards = {r["card_id"] for r in self._conn.execute("SELECT card_id FROM cards")}
        team = self._team_card_ids()
        drop = []
        for row in rows:
            key, value = row["key"], row["value"]
            if key.startswith("whoq:"):
                # вопрос «это ты написал?» с текстом сообщения — на сутки
                try:
                    at = json.loads(value).get("at")
                except ValueError:
                    at = None
                if not at or datetime.fromisoformat(at) < day_ago:
                    drop.append(key)
            elif key.startswith("who:"):
                when = value.partition("@")[2]
                if not when or datetime.fromisoformat(when) < day_ago:
                    drop.append(key)
            elif key.startswith("last_variant:"):
                contact = key.split(":", 2)[2]
                if contact not in chats:
                    drop.append(key)
            else:
                card = key.split(":", 1)[1]
                card_id = int(card) if card.isdigit() else None
                if key.startswith("fill:"):
                    if card_id not in cards and card_id not in team:
                        drop.append(key)
                elif card_id not in cards:
                    drop.append(key)  # кеш контактов по чужим заявкам — Kaiten отдаст его снова
        for key in drop:
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        return len(drop)

    def reschedule(self, msg_id: int, send_at: datetime) -> None:
        self._exec("UPDATE outbox SET send_at = ? WHERE id = ?", (_iso(send_at), msg_id))

    # --- чаты (для Business-режима) ---
    def remember_chat(self, keys: list[str], chat_id: int, last_in_at: datetime | None,
                      last_msg_id: int | None, name: str | None = None,
                      last_out_at: datetime | None = None) -> None:
        for key in keys:
            self._exec(
                "INSERT INTO chats(key, chat_id, last_in_at, last_msg_id, name, last_out_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET chat_id = excluded.chat_id, "
                "last_in_at = COALESCE(excluded.last_in_at, chats.last_in_at), "
                "last_msg_id = COALESCE(excluded.last_msg_id, chats.last_msg_id), "
                "name = COALESCE(excluded.name, chats.name), "
                "last_out_at = COALESCE(excluded.last_out_at, chats.last_out_at)",
                (key, chat_id, _iso(last_in_at) if last_in_at else None, last_msg_id, name,
                 _iso(last_out_at) if last_out_at else None),
            )

    def known_chats(self) -> list[dict]:
        """Один ряд на чат: chat_id, имя, @username, когда писал последний раз."""
        rows = self._exec("SELECT * FROM chats ORDER BY last_in_at DESC")
        chats: dict[int, dict] = {}
        for row in rows:
            item = chats.setdefault(row["chat_id"], {"chat_id": row["chat_id"], "name": row["name"],
                                                     "username": None, "last_in_at": _dt(row["last_in_at"])})
            if row["key"].startswith("username:"):
                item["username"] = row["key"].split(":", 1)[1]
            item["name"] = item["name"] or row["name"]
        return sorted(chats.values(), key=lambda c: c["last_in_at"] or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)

    def find_chat(self, key: str) -> dict | None:
        rows = self._exec("SELECT * FROM chats WHERE key = ?", (key,))
        if not rows:
            return None
        row = dict(rows[0])
        row["last_in_at"] = _dt(row["last_in_at"])
        row["last_out_at"] = _dt(row.get("last_out_at"))
        return row

    # --- key/value ---
    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self._exec("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def with_prefix(self, prefix: str) -> dict[str, str]:
        """Все непустые значения kv, ключ которых начинается с prefix."""
        rows = self._exec("SELECT key, value FROM kv WHERE substr(key, 1, ?) = ? AND value != ''",
                          (len(prefix), prefix))
        return {r["key"]: r["value"] for r in rows}

    def set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

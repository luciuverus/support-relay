"""Как сделать, чтобы сообщения выглядели написанными руками."""
from __future__ import annotations

import json
import random
import shutil
import string
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REQUIRED_KINDS = ("kaiten_comment", "employee_taken", "client_taken")
ALLOWED_FIELDS = {"title", "card_id", "deadline", "deadline_time", "deadline_full", "greeting", "name", "eta"}


class TemplateError(ValueError):
    pass


def _fields(template: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as exc:  # например, одиночная фигурная скобка
        raise TemplateError(f"сломанный шаблон {template!r}: {exc}") from exc


class MessageBook:
    """Тексты из messages.json.

    У каждого вида — список вариантов. Пустой список = это действие выключено
    (например, "client_taken": [] — клиенту не пишем). Один и тот же вариант
    одному человеку подряд не повторяется.

    Вариант — строка (одно сообщение) или список строк (несколько сообщений подряд,
    как когда пишешь приветствие, номер заявки и представление отдельными «пузырями»).
    """

    def __init__(self, variants: dict, rng: random.Random | None = None, defaults: dict | None = None):
        if not isinstance(variants, dict):
            raise TemplateError("messages.json должен быть объектом {...}")
        if defaults:
            # разделы, которых нет в твоём файле (например, новый «followup»), берём из текстов по умолчанию
            variants = {**{k: v for k, v in defaults.items() if not k.startswith("_")}, **variants}
        for kind in REQUIRED_KINDS:
            if kind not in variants:
                raise TemplateError(f"в messages.json нет раздела {kind!r} "
                                    "(чтобы выключить действие, оставь пустой список [])")
        clean: dict[str, list[list[str]]] = {}
        for kind, items in variants.items():
            if kind.startswith("_"):
                continue
            if not isinstance(items, list):
                raise TemplateError(f"{kind}: нужен список вариантов в квадратных скобках [...]")
            parsed: list[list[str]] = []
            for item in items:
                bubbles = item if isinstance(item, list) else [item]
                if not bubbles or not all(isinstance(b, str) and b.strip() for b in bubbles):
                    raise TemplateError(f"{kind}: вариант должен быть непустой строкой "
                                        f"или списком непустых строк, сейчас: {item!r}")
                for bubble in bubbles:
                    if "" in _fields(bubble):
                        raise TemplateError(f"{kind}: пустые скобки {{}} в {bubble!r} — укажи имя поля")
                    unknown = _fields(bubble) - ALLOWED_FIELDS
                    if unknown:
                        raise TemplateError(f"{kind}: неизвестные поля {sorted(unknown)} в {bubble!r}. "
                                            f"Можно: {', '.join(sorted(ALLOWED_FIELDS))}")
                parsed.append([b.strip() for b in bubbles])
            clean[kind] = parsed
        self.variants = clean
        self.rng = rng or random.Random()

    @classmethod
    def load(cls, path: str | Path, rng: random.Random | None = None,
             defaults: dict | None = None) -> "MessageBook":
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise TemplateError(f"ошибка JSON в строке {exc.lineno}, символ {exc.colno}: {exc.msg}") from exc
        except OSError as exc:
            raise TemplateError(f"не открыть {path}: {exc}") from exc
        return cls(data, rng, defaults)

    def enabled(self, kind: str) -> bool:
        return bool(self.variants.get(kind))

    def render(self, kind: str, values: dict,
               last_index: int | None = None) -> tuple[list[str], int] | None:
        """Сообщения («пузыри») выбранного варианта и его номер, или None, если действие выключено."""
        items = self.variants.get(kind) or []
        if not items:
            return None
        choices = [i for i in range(len(items)) if i != last_index] or list(range(len(items)))
        index = self.rng.choice(choices)
        return [bubble.format(**values) for bubble in items[index]], index


class MessageStore:
    """Держит актуальный MessageBook и перечитывает файл, когда он изменился — без перезапуска.

    Если новый файл с ошибкой, продолжаем работать на прежних текстах, а ошибку
    отдаём один раз через pop_error(), чтобы сервис мог предупредить тебя.
    """

    def __init__(self, path: str | Path, rng: random.Random | None = None,
                 default_path: str | Path | None = None):
        self.path = Path(path)
        self.rng = rng or random.Random()
        self.defaults: dict | None = None
        if default_path and Path(default_path).exists():
            with open(default_path, encoding="utf-8") as fh:
                self.defaults = json.load(fh)
            if not self.path.exists():  # первый запуск: твой файл создаётся из текстов по умолчанию
                self.path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(default_path, self.path)
        self._book = MessageBook.load(self.path, self.rng, self.defaults)  # ошибка при старте — сразу падаем
        self._stamp = self._file_stamp()
        self._pending_error: str | None = None

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    @property
    def book(self) -> MessageBook:
        stamp = self._file_stamp()
        if stamp is not None and stamp != self._stamp:
            self._stamp = stamp
            try:
                self._book = MessageBook.load(self.path, self.rng, self.defaults)
                self._pending_error = None
            except TemplateError as exc:
                self._pending_error = str(exc)
        return self._book

    def pop_error(self) -> str | None:
        error, self._pending_error = self._pending_error, None
        return error


def greeting(local_now: datetime) -> str:
    hour = local_now.hour
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    return "Добрый вечер"


def typing_seconds(text: str, rng: random.Random, cps_min: float, cps_max: float,
                   cap: float = 45.0) -> float:
    """Сколько «печатать»: длина текста / скорость набора, в разумных пределах (не дольше cap)."""
    cps = rng.uniform(cps_min, cps_max)
    return max(min(2.0, cap), min(len(text) / cps, cap))


@dataclass(frozen=True)
class Pace:
    """Темп сообщений: пауза перед первым сообщением и скорость «набора»."""
    key: str
    label: str
    delay_min_s: float
    delay_max_s: float
    cps_min: float
    cps_max: float
    typing_max_s: float  # дольше этого «печатает…» на одно сообщение не держим
    stagger: tuple[float, float] = (20.0, 70.0)  # пауза перед вторым человеком по той же заявке

    def describe(self) -> str:
        return (f"{self.label}: пауза {self.delay_min_s:g}–{self.delay_max_s:g} с, "
                f"«печатает…» до {self.typing_max_s:g} с на сообщение")


SPEEDS = {
    "instant": Pace("instant", "⚡ мгновенно", 0, 5, 25, 35, 3, (3.0, 8.0)),
    "fast": Pace("fast", "🚀 быстро", 10, 30, 12, 18, 10, (8.0, 20.0)),
    "slow": Pace("slow", "🐢 спокойно (как раньше)", 40, 150, 4, 7, 45),
}
SPEED_KEYS = (*SPEEDS, "custom")  # custom — HUMAN_DELAY_* и TYPING_CPS_* из .env


def _is_work_moment(local: datetime, start: time, end: time, days: frozenset[int]) -> bool:
    return local.isoweekday() in days and start <= local.time() < end


def next_send_time(
    now: datetime,
    tz: ZoneInfo,
    start: time,
    end: time,
    days: frozenset[int],
    rng: random.Random,
    delay_min_s: int,
    delay_max_s: int,
) -> datetime:
    """Небольшая «человеческая» пауза; вне рабочего времени — начало следующего рабочего дня."""
    candidate = now + timedelta(seconds=rng.uniform(delay_min_s, delay_max_s))
    local = candidate.astimezone(tz)
    if _is_work_moment(local, start, end, days):
        return candidate
    day = local.date()
    if local.time() >= end or local.isoweekday() not in days:
        day += timedelta(days=1)
    for _ in range(8):
        if datetime(day.year, day.month, day.day, tzinfo=tz).isoweekday() in days:
            break
        day += timedelta(days=1)
    opening = datetime.combine(day, start, tzinfo=tz)
    return opening + timedelta(seconds=rng.uniform(60, 15 * 60))

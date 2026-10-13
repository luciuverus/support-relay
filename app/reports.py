"""Отчёт руководителю за неделю и за месяц: что сделал бот (по журналу /log) и как работала команда.

Неделя — в понедельник после начала рабочего дня, за прошлые пн–вс.
Месяц — 1-го числа после начала рабочего дня, за прошлый месяц.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median

log = logging.getLogger(__name__)

# (что ищем в начале строки журнала, название в отчёте, минут экономит одно действие)
ACTIONS: list[tuple[str, str, float]] = [
    ("взял в работу", "взял заявок в работу", 1.0),
    ("написал автору", "поздоровался с автором", 1.5),
    ("написал клиенту", "поздоровался с клиентом", 1.5),
    ("написал «актуально ли?»", "спросил «актуально ли?»", 1.0),
    ("перенёс в «В работе»", "вернул в «В работе»", 0.25),
    ("перенёс в «Ждём ответа»", "перенёс в «Ждём ответа»", 0.25),
    ("перенёс в «Ожидание информации для оценки»", "перенёс в «Ожидание информации для оценки»", 0.25),
    ("вернул в «Новую» — клиент ответил", "вернул в «Новую» после ответа клиента", 0.25),
    ("в работе по команде", "взял в работу по команде и написал время решения", 0.5),
    ("закрыл после «оцените работу»", "закрыл после оценки", 0.75),
    ("закрыл: клиент не ответил", "закрыл «клиент не ответил»", 1.0),
    ("записал категорию", "заполнил фактическую категорию", 0.25),
    ("записал «что сделали»", "заполнил «что сделали»", 0.0),
    ("до SLA", "напомнил о сроке SLA", 0.0),
    ("SLA просрочен", "SLA просрочен", 0.0),
]
PROBLEMS = [("не смог написать", "не смог написать (написал ты)"),
            ("жду, пока", "ждал, пока человек напишет первым"),
            ("ты написал", "ты написал сам — бот своё отменил")]
def team_source_note(cfg) -> str:
    """Цифры команды бот считает сам (опрос доски), а точные SLA — в дашборде. Пишем это рядом с цифрами."""
    url = getattr(cfg, "dashboard_url", "")
    where = f"в дашборде: {url}" if url else "в дашборде SLA/KPI"
    return (f"ℹ️ Цифры команды — по данным бота (опрос доски раз в минуту, без пауз и рабочего календаря). "
            f"Точные SLA и KPI — {where}")


MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]


@dataclass
class Period:
    kind: str           # "week" | "month"
    since: datetime     # включительно
    until: datetime     # не включительно
    title: str
    key: str            # чтобы не отправить дважды


class Reporter:
    def __init__(self, relay, detector=None, team=None):
        self.relay = relay
        self.detector = detector
        self.team = team  # вся команда на боте: в отчёт главного — действия бота у всех
        self.cfg = relay.cfg
        self.storage = relay.storage

    # --- периоды ---
    def _local_midnight(self, day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=self.cfg.tz)

    def period(self, kind: str, today: date, previous: bool = True) -> Period:
        if kind == "week":
            monday = today - timedelta(days=today.weekday())
            if previous:
                monday -= timedelta(days=7)
            start, end = monday, monday + timedelta(days=7)
            title = f"неделю {start:%d.%m}–{end - timedelta(days=1):%d.%m}"
            key = f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}"
        else:
            first = today.replace(day=1)
            if previous:
                first = (first - timedelta(days=1)).replace(day=1)
            nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            start, end = first, nxt
            title = f"{MONTHS[first.month - 1]} {first.year}"
            key = f"{first:%Y-%m}"
        return Period(kind, self._local_midnight(start), self._local_midnight(end), title, key)

    # --- текст ---
    def text(self, period: Period) -> str:
        # записи тестового режима начинаются с «[тест]» и ни с одним действием не совпадают
        storages = self.team.storages if self.team is not None else [self.storage]
        rows = [r for st in storages for r in st.journal_between(period.since, period.until)]
        counts: dict[str, int] = {}
        minutes = 0.0
        for r in rows:
            for prefix, label, cost in ACTIONS:
                if r["action"].startswith(prefix):
                    counts[label] = counts.get(label, 0) + 1
                    minutes += cost
                    break
        problems: dict[str, int] = {}
        for r in rows:
            for prefix, label in PROBLEMS:
                if r["action"].startswith(prefix):
                    problems[label] = problems.get(label, 0) + 1
                    break
        lines = [f"📈 Отчёт за {period.title}", "", "🤖 Что сделал бот:"]
        if counts:
            for _, label, _ in ACTIONS:
                if counts.get(label):
                    lines.append(f"• {label}: {counts[label]}")
            hours = minutes / 60
            lines.append(f"≈ сэкономлено {hours:.1f} ч ручной работы (оценка по типичному времени действий)")
        else:
            lines.append("• действий нет (бот был выключен или заявок не было)")
        if problems:
            lines.append("")
            lines.append("⚠️ Где понадобился человек:")
            lines += [f"• {label}: {n}" for label, n in problems.items()]
        team = self._team_lines(period)
        if team:
            lines += ["", "👥 Команда (по данным бота):", *team, team_source_note(self.cfg)]
        lessons = getattr(self.relay, "lessons", None)
        checks = lessons.summary_lines(period.since, period.until) if lessons else []
        if checks:
            lines += ["", "🩺 Проверки ПК перед уроком:", *checks]
        lines += ["", "Данные — за время, пока работал сервис."]
        return "\n".join(lines)

    def _team_lines(self, period: Period) -> list[str]:
        created = [r for r in self.storage.team_created_since(period.since) if r["created_at"] < period.until]
        left = [r for r in self.storage.team_left_since(period.since) if r["left_queue_at"] < period.until]
        if not created and not left:
            return []
        lines = []
        by_lane: dict[str, int] = {}
        for r in created:
            by_lane[r["lane"] or "—"] = by_lane.get(r["lane"] or "—", 0) + 1
        lines.append(f"• новых заявок: {len(created)}" + (
            " — " + ", ".join(f"{k} {v}" for k, v in sorted(by_lane.items(), key=lambda x: -x[1])) if by_lane else ""))
        waits = [(r, max((r["left_queue_at"] - r["entered_at"]).total_seconds() / 60, 0.0)) for r in left]
        if waits:
            fast = sum(1 for _, w in waits if w <= 5)
            lines.append(f"• взято за 5 минут: {round(100 * fast / len(waits))}% ({fast} из {len(waits)}), "
                         f"медиана ожидания {median(w for _, w in waits):.0f} мин")
            if self.cfg.digest_by_person:
                people: dict[str, list[float]] = {}
                for r, w in waits:
                    people.setdefault(r["responsible"] or "без ответственного", []).append(w)
                for who, ws in sorted(people.items(), key=lambda x: -len(x[1])):
                    late = sum(1 for w in ws if w > 5)
                    lines.append(f"  – {who}: {len(ws)}, медиана {median(ws):.0f} мин"
                                 + (f", позже 5 мин — {late}" if late else ""))
        if self.detector:
            incidents = [e for e in self.detector.incidents_since(period.since)
                         if datetime.fromisoformat(e["at"]) < period.until]
            lines.append(f"• массовых сбоев и завалов: {len(incidents)}")
        return lines

    # --- отправка по расписанию ---
    async def tick(self) -> list[str]:
        now = self.relay.clock().astimezone(self.cfg.tz)
        if now.time() < self.cfg.work_start:
            return []
        sent = []
        jobs = []
        if self.cfg.report_weekly and now.weekday() == 0:
            jobs.append(self.period("week", now.date()))
        if self.cfg.report_monthly and now.day == 1:
            jobs.append(self.period("month", now.date()))
        for period in jobs:
            flag = f"report:{period.kind}"
            if self.storage.get(flag) == period.key:
                continue
            self.storage.set(flag, period.key)
            text = self.text(period)
            await self.relay.alert.send(text)
            if self.detector:
                await self.detector.to_manager(text)
            sent.append(f"{period.kind}:{period.key}")
        return sent

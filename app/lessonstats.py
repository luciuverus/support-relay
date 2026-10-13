"""Статистика проверок перед уроком (программа lesson-check и страница проверки).

В конце каждого отчёта есть служебная строка:
  #lesson-check 1.4 problems=cam.privacy,snd.muted left=snd.muted   — программа на ПК
  #lesson-check 1.4 problems=- left=- crashed=1                    — программа упала
  #lesson-check 1.4 deep=1 steps=3                                 — глубокий ремонт
  #lesson-check-web 1 problems=web.cam.perm,web.net.netSlow        — страница в браузере
Её находим в комментариях карточек Kaiten (туда вставляют отчёт) и в сообщениях,
пришедших на корпоративный аккаунт. Храним только коды, дату и номер карточки — без текста отчёта.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# коды программы lesson-check ($script:FindingCodes) → что это по-русски
PC_LABELS: dict[str, str] = {
    "devices.query": "не удалось получить список устройств",
    "devices.driver": "устройство с ошибкой драйвера",
    "cam.none": "камера не найдена",
    "cam.policy": "камера запрещена политикой",
    "cam.policy_apps": "политика запрещает приложениям камеру",
    "cam.service": "служба камеры отключена",
    "cam.privacy": "камера запрещена в конфиденциальности",
    "cam.disabled": "камера отключена в диспетчере",
    "cam.driver": "ошибка драйвера камеры",
    "cam.busy": "камеру держит другая программа",
    "snd.none": "нет ни одного устройства звука",
    "snd.no_output": "нет динамиков/наушников",
    "snd.disabled": "звук отключён в Windows",
    "snd.driver": "ошибка драйвера звука",
    "snd.service": "служба звука не работает",
    "snd.muted": "звук выключен",
    "snd.quiet": "звук почти на нуле",
    "snd.handsfree": "Bluetooth-наушники в режиме Hands-Free",
    "mic.none": "микрофон не найден",
    "mic.disabled": "микрофон отключён",
    "mic.muted": "микрофон выключен",
    "mic.quiet": "микрофон почти на нуле",
    "mic.virtual": "выбран виртуальный микрофон",
    "mic.no_default": "микрофон по умолчанию не выбран",
    "mic.policy": "политика запрещает приложениям микрофон",
    "mic.privacy": "микрофон запрещён в конфиденциальности",
    "mic.busy": "микрофон занят другой программой",
    "apps.open": "открыты программы, которые держат камеру/микрофон",
    "net.offline": "нет интернета",
    "net.dns": "не работает DNS",
    "net.latency": "высокая задержка сети",
    "net.speed_unknown": "скорость не измерилась",
    "net.slow": "очень медленный интернет",
    "net.slowish": "интернет может тормозить",
    "net.proxy": "включён прокси",
    "net.vpn": "включён VPN",
    "av.block": "антивирус блокирует камеру/микрофон",
    "time.skew": "неправильное время на ПК",
    "wifi.very_weak": "очень слабый Wi-Fi",
    "wifi.weak": "слабый Wi-Fi",
    "pc.reboot_pending": "ПК ждёт перезагрузки",
    "pc.uptime": "ПК давно не перезагружался",
    "pc.ram": "мало свободной памяти",
    "pc.cpu": "процессор загружен",
    "pc.old_windows": "старая Windows",
    "pc.disk": "мало места на диске",
    "br.old": "устаревший браузер",
    "br.site_block": "сайт урока запрещён в браузере",
    "br.cache": "большой кэш браузера",
    "mictest.silent": "тест микрофона: тишина",
    "mictest.quiet": "тест микрофона: почти не слышно",
    "mictest.error": "тест микрофона не удался",
    "other": "другое",
}

# страница: web.<шаг>.<подсказка>
WEB_STEPS = {"env": "браузер", "cam": "камера", "mic": "микрофон", "spk": "звук", "net": "интернет"}
WEB_HINTS = {
    "insecure": "открыта не по https", "inapp": "открыта внутри приложения",
    "unsupported": "браузер без видеосвязи", "oldBrowser": "устаревший браузер",
    "perm": "нет разрешения", "camBusy": "занята другой программой", "camMissing": "не найдена",
    "camSlow": "мало кадров", "camDark": "тёмная картинка", "micSilent": "не слышит голос",
    "micMissing": "не найден", "micBusy": "занят другой программой", "spkNo": "не слышно",
    "netOffline": "нет интернета", "netSlow": "медленный", "netUnknown": "скорость не измерилась",
    "netUdp": "сеть не пропускает видеосвязь (UDP)", "warn": "замечание", "bad": "проблема",
}

QUICK_RE = re.compile(r"lesson\\?-check")  # быстрый отсев; в Kaiten дефис бывает экранирован
TAG_RE = re.compile(r"#lesson-check(?P<web>-web)?[ \t]+(?P<ver>\d[\w.]{0,15})(?P<rest>[^\n]*)")
CODE_RE = re.compile(r"^[a-z][a-z_]*(\.[A-Za-z_]+){1,2}$|^other$")
MAX_CODES = 60
# markdown-экранирование, которое добавляет редактор комментариев: \# \- \_ \= …
MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!=|,])")
HTML_BREAK = re.compile(r"<\s*(br|/p|/div|/li)\s*/?\s*>", re.I)
HTML_TAG = re.compile(r"<[^>]{0,200}>")


def label(code: str) -> str:
    if code in PC_LABELS:
        return PC_LABELS[code]
    parts = code.split(".")
    if len(parts) == 3 and parts[0] == "web":
        step = WEB_STEPS.get(parts[1], parts[1])
        return f"страница: {step} — {WEB_HINTS.get(parts[2], parts[2])}"
    return code


def clean_text(text: str) -> str:
    """Текст комментария Kaiten (HTML или markdown) → обычные строки."""
    text = HTML_BREAK.sub("\n", text or "")
    text = HTML_TAG.sub("", text)
    text = html.unescape(text).replace(" ", " ")
    return MD_ESCAPE.sub(r"\1", text)


def _codes(value: str | None) -> list[str] | None:
    if value is None:
        return None
    if value in ("", "-"):
        return []
    out: list[str] = []
    for code in value.split(","):
        code = code.strip()
        if CODE_RE.match(code) and len(code) <= 40 and code not in out:
            out.append(code)
    return out[:MAX_CODES]


@dataclass
class Check:
    source: str                 # "pc" | "web" | "deep"
    version: str
    problems: list[str] = field(default_factory=list)
    left: list[str] | None = None   # только у программы: что осталось после исправлений
    crashed: bool = False
    steps: int = 0              # глубокий ремонт: сколько шагов сделано
    line: str = ""

    @property
    def fixed(self) -> list[str]:
        if self.left is None:
            return []
        return [c for c in self.problems if c not in self.left]


def parse(text: str) -> list[Check]:
    """Все служебные строки в тексте (в одном комментарии может быть несколько отчётов)."""
    checks: list[Check] = []
    for match in TAG_RE.finditer(clean_text(text)):
        pairs = dict(re.findall(r"(\w+)=(\S*)", match.group("rest")))
        version = match.group("ver").rstrip(".")
        line = match.group(0).strip()
        if match.group("web"):
            problems = _codes(pairs.get("problems"))
            if problems is None:
                continue
            checks.append(Check("web", version, problems, None, line=line))
        elif pairs.get("deep") == "1":
            try:
                steps = max(0, min(int(pairs.get("steps", "0")), 99))
            except ValueError:
                steps = 0
            checks.append(Check("deep", version, [], None, steps=steps, line=line))
        else:
            problems = _codes(pairs.get("problems"))
            if problems is None:
                continue
            left = _codes(pairs.get("left"))
            if left is not None:  # «осталось» всегда входит в «было»
                problems = problems + [c for c in left if c not in problems]
            checks.append(Check("pc", version, problems, left, crashed=pairs.get("crashed") == "1", line=line))
    return checks


# --- хранение ---
SCHEMA = """
CREATE TABLE IF NOT EXISTS lesson_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    card_id INTEGER,
    origin TEXT NOT NULL,
    source TEXT NOT NULL,
    version TEXT NOT NULL,
    problems TEXT NOT NULL,
    left_codes TEXT,
    crashed INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lesson_checks_at ON lesson_checks(at);
CREATE TABLE IF NOT EXISTS lesson_scans (
    card_id INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL
);
"""

SCAN_BATCH = 40                  # карточек за один проход (бережём лимиты Kaiten)
SCAN_MAX_AGE = timedelta(days=8)  # старше — уже не перечитываем
RESCAN_FRESH = timedelta(hours=6)   # карточка моложе 2 дней
RESCAN_OLD = timedelta(hours=20)
SCAN_INTERVAL_S = 15 * 60


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class LessonStats:
    def __init__(self, storage, kaiten, cfg, clock, export_dir: str | None = None):
        self.storage = storage
        self.kaiten = kaiten
        self.cfg = cfg
        self.clock = clock
        if export_dir is None:
            db = getattr(cfg, "db_path", ":memory:")
            export_dir = str(Path(db).parent) if db != ":memory:" else ""
        self.export_dir = export_dir
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    # --- запись ---
    def _known(self, card_id: int | None, line: str) -> bool:
        if card_id is None:
            return False
        rows = self.storage._exec("SELECT 1 FROM lesson_checks WHERE card_id = ? AND fingerprint LIKE ?",
                                  (card_id, "%|" + hashlib.sha1(line.encode()).hexdigest()[:16]))
        return bool(rows)

    def record(self, checks: list[Check], at: datetime, origin: str, card_id: int | None, key: str) -> int:
        """key — откуда (комментарий, сообщение): та же строка из того же места не считается дважды."""
        added = 0
        for i, check in enumerate(checks):
            line_hash = hashlib.sha1(check.line.encode()).hexdigest()[:16]
            fp = f"{origin}:{hashlib.sha1(key.encode()).hexdigest()[:16]}:{i}|{line_hash}"
            # один отчёт прислали в чат и вставили в карточку — это одна проверка
            if self._known(card_id, check.line):
                continue
            with self.storage._lock:
                cur = self.storage._conn.execute(
                    "INSERT OR IGNORE INTO lesson_checks(fingerprint, at, card_id, origin, source, version, "
                    "problems, left_codes, crashed, steps) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fp, _iso(at), card_id, origin, check.source, check.version, ",".join(check.problems),
                     None if check.left is None else ",".join(check.left), int(check.crashed), check.steps))
                self.storage._conn.commit()
            added += cur.rowcount
        if added:
            self.export_csv()
        return added

    async def on_chat_text(self, text: str, at: datetime, key: str, find_card=None) -> int:
        """Сообщение на корпоративный аккаунт. find_card() → номер карточки этого человека или None."""
        if "#lesson-check" not in (text or "") and "\\#lesson" not in (text or ""):
            return 0
        checks = parse(text)
        if not checks:
            return 0
        card_id = None
        if find_card is not None:
            try:
                card_id = await find_card()
            except Exception:  # noqa: BLE001 - статистика не должна ломать обработку сообщения
                log.exception("lesson-check: не нашёл карточку для сообщения")
        return self.record(checks, at, "chat", card_id, f"{key}|{at.isoformat()}|{text}")

    # --- комментарии Kaiten ---
    def _candidates(self, now: datetime) -> list[int]:
        since = _iso(now - SCAN_MAX_AGE)
        rows = self.storage._exec(
            "SELECT card_id, MIN(born) AS born FROM ("
            " SELECT card_id, taken_at AS born FROM cards WHERE taken_at >= ?"
            " UNION ALL SELECT card_id, created_at AS born FROM team_cards WHERE created_at >= ?"
            ") GROUP BY card_id", (since, since))
        scans = {r["card_id"]: datetime.fromisoformat(r["scanned_at"])
                 for r in self.storage._exec("SELECT card_id, scanned_at FROM lesson_scans")}
        due: list[tuple[int, datetime, int]] = []
        for r in rows:
            born = datetime.fromisoformat(r["born"])
            last = scans.get(r["card_id"])
            if last is not None:
                gap = RESCAN_FRESH if now - born < timedelta(days=2) else RESCAN_OLD
                if now - last < gap:
                    continue
            # сначала ни разу не прочитанные, потом давно прочитанные
            due.append((0 if last is None else 1, last or born, r["card_id"]))
        due.sort()
        return [card_id for _, _, card_id in due[:SCAN_BATCH]]

    async def scan(self) -> int:
        now = self.clock()
        added = 0
        for card_id in self._candidates(now):
            try:
                comments = await self.kaiten.list_comments(card_id)
            except Exception as exc:  # noqa: BLE001 - одна карточка не должна останавливать проход
                # попробуем снова в следующий плановый раз, а не каждые 15 минут
                log.warning("lesson-check: комментарии %s: %s", card_id, exc)
                comments = []
            for comment in comments:
                text = str(comment.get("text") or "")
                if not QUICK_RE.search(text):
                    continue
                checks = parse(text)
                if not checks:
                    continue
                at = _comment_time(comment) or now
                added += self.record(checks, at, "kaiten", card_id, f"{card_id}:{comment.get('id')}")
            self.storage._exec("INSERT OR REPLACE INTO lesson_scans(card_id, scanned_at) VALUES (?, ?)",
                               (card_id, _iso(now)))
        self.storage._exec("DELETE FROM lesson_scans WHERE scanned_at < ?", (_iso(now - 2 * SCAN_MAX_AGE),))
        return added

    # --- чтение ---
    def rows(self, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        sql, params = "SELECT * FROM lesson_checks WHERE 1=1", []
        if since is not None:
            sql += " AND at >= ?"
            params.append(_iso(since))
        if until is not None:
            sql += " AND at < ?"
            params.append(_iso(until))
        out = []
        for r in self.storage._exec(sql + " ORDER BY at, id", tuple(params)):
            d = dict(r)
            d["at"] = datetime.fromisoformat(d["at"])
            d["problems"] = [c for c in d["problems"].split(",") if c]
            d["left"] = None if d.pop("left_codes") is None else [c for c in (r["left_codes"] or "").split(",") if c]
            out.append(d)
        return out

    def summary_lines(self, since: datetime, until: datetime, top: int = 5) -> list[str]:
        """Короткий блок для недельного/месячного отчёта ([] если проверок не было)."""
        rows = [r for r in self.rows(since, until) if r["source"] != "deep"]
        if not rows:
            return []
        counts = _count(rows)
        pc = sum(1 for r in rows if r["source"] == "pc")
        lines = [f"• проверок: {len(rows)} (программа {pc}, страница {len(rows) - pc})"]
        if counts:
            lines.append("• чаще всего: " + "; ".join(f"{label(c)} — {n}" for c, n in counts[:top]))
        return lines

    def text(self, since: datetime, until: datetime, title: str) -> str:
        all_rows = self.rows(since, until)
        rows = [r for r in all_rows if r["source"] != "deep"]
        deep = [r for r in all_rows if r["source"] == "deep"]
        if not all_rows:
            return (f"🩺 Проверки перед уроком за {title}: пока ни одной.\n"
                    "Считаю по строке «#lesson-check …» в конце отчёта — в комментариях карточек и в чатах.")
        pc = [r for r in rows if r["source"] == "pc"]
        lines = [f"🩺 Проверки перед уроком за {title}: {len(rows)} (программа {len(pc)}, страница {len(rows) - len(pc)})"]
        clean = sum(1 for r in rows if not r["problems"])
        if rows:
            lines.append(f"Всё в порядке сразу: {clean} ({_pct(clean, len(rows))})")
        counts = _count(rows)
        if counts:
            lines += ["", "Чаще всего:"]
            lines += [f"• {label(c)} — {n} ({_pct(n, len(rows))})" for c, n in counts[:10]]
        fixed: dict[str, int] = {}
        left: dict[str, int] = {}
        for r in pc:
            if r["left"] is None:
                continue
            for c in r["problems"]:
                bucket = left if c in r["left"] else fixed
                bucket[c] = bucket.get(c, 0) + 1
        if fixed:
            lines += ["", "Программа исправила на месте:"]
            lines += [f"• {label(c)} — {n}" for c, n in sorted(fixed.items(), key=lambda x: (-x[1], x[0]))[:8]]
        if left:
            lines += ["", "Не исправилось (нужен человек):"]
            lines += [f"• {label(c)} — {n}" for c, n in sorted(left.items(), key=lambda x: (-x[1], x[0]))[:8]]
        crashed = sum(1 for r in pc if r["crashed"])
        extra = []
        if deep:
            extra.append(f"глубокий ремонт: {len(deep)}")
        if crashed:
            extra.append(f"программа упала: {crashed} (отчёт с ошибкой — разработчику)")
        if extra:
            lines += ["", "• " + "\n• ".join(extra)]
        if self.export_dir:
            lines += ["", "Таблица для дашборда: /checks csv"]
        return "\n".join(lines)

    # --- выгрузка ---
    def csv_text(self) -> str:
        """Одна строка на (проверка, проблема) — удобно для сводной таблицы. Без текста отчётов и контактов."""
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        writer.writerow(["дата", "проверка", "откуда", "версия", "карточка", "код", "проблема", "исправлено"])
        tz = getattr(self.cfg, "tz", timezone.utc)
        for r in self.rows():
            day = r["at"].astimezone(tz).strftime("%Y-%m-%d")
            source = {"pc": "программа", "web": "страница", "deep": "глубокий ремонт"}[r["source"]]
            base = [day, r["id"], source, r["version"], r["card_id"] or ""]
            if r["source"] == "deep":
                writer.writerow(base + ["deep", f"шагов: {r['steps']}", ""])
                continue
            if not r["problems"]:
                writer.writerow(base + ["-", "всё в порядке", ""])
                continue
            for c in r["problems"]:
                done = "" if r["left"] is None else ("нет" if c in r["left"] else "да")
                writer.writerow(base + [c, label(c), done])
        return buf.getvalue()

    def csv_path(self) -> Path | None:
        return Path(self.export_dir) / "lesson-checks.csv" if self.export_dir else None

    def export_csv(self) -> Path | None:
        path = self.csv_path()
        if path is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(b"\xef\xbb\xbf" + self.csv_text().encode("utf-8"))
            tmp.replace(path)  # Excel держит файл открытым — тогда просто не обновим до следующего раза
        except OSError as exc:
            log.warning("lesson-check: не записал %s: %s", path, exc)
            return None
        return path


def _comment_time(comment: dict) -> datetime | None:
    raw = comment.get("created") or comment.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _count(rows: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for r in rows:
        for c in r["problems"]:
            counts[c] = counts.get(c, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total)}%" if total else "0%"

"""Похожие решённые заявки: «такое уже было — тогда помогло вот это».

База случаев собирается из выгрузки Kaiten (сводный отчёт, xlsx): берутся заявки, где заполнено
поле «Фактическая категория проблемы» — это и есть «что было на самом деле / что сделали».
Поиск — BM25 по словам заголовка, описания и ответов формы; всё считается на твоём ноутбуке,
никуда не отправляется.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

WORD = re.compile(r"[a-zа-я0-9]+")
STEM = 6  # грубое «основание» слова: первые 6 букв (оплата/оплаты/оплатить → оплата/оплати…)
STOP = set("""и в во не на с со по к ко у о об от до за из для что как это то так но а или же ли бы
был была было были есть нет да уже еще ещё только очень все всё его ее её их им ей мы вы он она они
я ты мне меня нам вам вас нас там тут при про под над без чтобы если когда где который которая
которые может можно нужно надо просто вот здравствуйте добрый день пожалуйста спасибо подскажите
помогите проблема проблемы вопрос заявка заявку ученик ученика ученику учитель учителя клиент клиента
клиентка написал написала пишет пишут другое напишу комментариях""".split())
# Поля формы, которые описывают проблему (по названию колонки в выгрузке)
FORM_HINTS = ("какая проблема", "какая у вас проблема", "с чем возникла проблема", "действия до передачи",
              "выберите сервис", "у кого возникла проблема")
# «Решения», из которых нечему научиться: автор пропал, само прошло
USELESS = re.compile(r"^(нет ответа|не отвеча|неактуал|не актуал|решилось само|сами|само|клиент не ответ|"
                     r"закрыт|дубл|тест|ошибочн)", re.I)
USELESS_CATEGORIES = {"решилось само/неактуально"}
EMPTY_ANSWERS = {"другое", "другое, напишу в комментариях", "другое (напишу в комментариях)", "-", "nan", ""}


def _norm(text: str) -> str:
    return str(text or "").casefold().replace("ё", "е")


def tokens(text: str) -> list[str]:
    out = []
    for word in WORD.findall(_norm(text)):
        if word in STOP or len(word) < 2 or word.isdigit():
            continue
        out.append(word[:STEM])
    return out


# «Фамилия Имя [Отчество]» — два-три слова подряд с заглавной буквы
_NAME = re.compile(r"\b[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,}){1,2}\b")


def _scrub(text: str) -> str:
    """Убрать персональные данные: ссылки, почты, @ники, телефоны и ФИО (в поиске они не нужны)."""
    text = re.sub(r"https?://\S+|t\.me/\S+", " ", str(text or ""))
    text = re.sub(r"\S+@\S+", " ", text)
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"\+?\d[\d\s()-]{7,}\d", " ", text)
    text = _NAME.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Case:
    card_id: int
    title: str
    problem: str      # описание + ответы формы (без личных данных)
    solution: str     # «Фактическая категория проблемы (Строка)»
    category: str     # «Фактическая категория проблемы (Селект)»
    lane: str
    created: str      # YYYY-MM-DD

    @property
    def text(self) -> str:
        return f"{self.title} {self.title} {self.problem}"


class CaseBase:
    K1, B = 1.4, 0.75

    def __init__(self, cases: list[Case]):
        self.cases = cases
        self._docs = [Counter(tokens(c.text)) for c in cases]
        self._len = [sum(d.values()) or 1 for d in self._docs]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for d in self._docs:
            df.update(d.keys())
        n = len(cases)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def __len__(self) -> int:
        return len(self.cases)

    def scores(self, text: str) -> list[tuple[float, int, int, float]]:
        """(score, индекс, сколько разных слов совпало, доля «веса» запроса, которая совпала)."""
        query = {t for t in tokens(text) if t in self._idf}
        total = sum(self._idf[t] for t in query) or 1.0
        result = []
        for i, doc in enumerate(self._docs):
            s, hit, matched = 0.0, 0.0, 0
            for t in query:
                f = doc.get(t)
                if f:
                    matched += 1
                    hit += self._idf[t]
                    s += self._idf[t] * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * self._len[i] / self._avg))
            if s > 0:
                result.append((s, i, matched, hit / total))
        result.sort(reverse=True)
        return result

    def search(self, text: str, k: int = 3, min_words: int = 2, min_cover: float = 0.25,
               exclude_id: int | None = None) -> list[Case]:
        """До k разных решений, самые похожие первыми.

        Слабые совпадения отбрасываются: нужно минимум 2 общих слова и ≥25% «веса» запроса
        (редкие слова вроде «иностранной» весят больше частых вроде «оплата»)."""
        found: list[Case] = []
        seen: set[str] = set()
        for _score, i, matched, cover in self.scores(text):
            if len(found) >= k:
                break
            if matched < min_words or cover < min_cover:
                continue
            case = self.cases[i]
            key = _norm(case.solution)[:40]
            if case.card_id == exclude_id or key in seen:
                continue
            seen.add(key)
            found.append(case)
        return found

    # --- файл базы ---
    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps([asdict(c) for c in self.cases], ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CaseBase | None":
        file = Path(path)
        if not file.exists():
            return None
        return cls([Case(**row) for row in json.loads(file.read_text(encoding="utf-8"))])


def format_hint(cases: list[Case], url_prefix: str = "") -> str:
    """Строки для уведомления: что помогало в похожих случаях."""
    if not cases:
        return ""
    lines = ["💡 Похожие случаи — тогда помогло:"]
    for c in cases:
        cat = f" [{c.category}]" if c.category and _norm(c.category) not in _norm(c.solution) else ""
        lines.append(f"• {c.solution[:120]}{cat} — «{c.title[:50]}», {c.created}"
                     + (f" {url_prefix}{c.card_id}" if url_prefix else f" №{c.card_id}"))
    return "\n".join(lines)


def card_query(card: dict) -> str:
    """Текст карточки Kaiten для поиска: заголовок, описание, строковые поля формы."""
    parts = [str(card.get("title") or ""), str(card.get("description") or "")]
    props = card.get("properties")
    if isinstance(props, dict):
        parts += [v for v in props.values() if isinstance(v, str) and _norm(v) not in EMPTY_ANSWERS]
    return _scrub(" ".join(parts))


def cases_from_rows(header: list[str], rows) -> list[Case]:
    """Строки сводного отчёта Kaiten → случаи (только с заполненным «что было на самом деле»)."""
    idx = {name: i for i, name in enumerate(header)}

    def col(*names: str) -> int | None:
        for name in names:
            if name in idx:
                return idx[name]
        return None

    c_id, c_title = col("ID"), col("Название")
    c_desc = col("Опишите вашу проблему подробно:")
    c_sol = col("Фактическая категория проблемы (Строка)")
    c_cat = col("Фактическая категория проблемы (Селект)")
    c_lane, c_created = col("Дорожка"), col("Дата создания")
    if c_id is None or c_title is None or c_sol is None:
        raise ValueError("в файле нет колонок «ID», «Название» и «Фактическая категория проблемы (Строка)» — "
                         "это точно сводный отчёт Kaiten?")
    form_cols = [i for name, i in idx.items() if any(h in _norm(name) for h in FORM_HINTS)]
    cases = []
    for row in rows:
        def get(i):
            if i is None or i >= len(row) or row[i] is None:
                return ""
            return str(row[i]).strip()
        solution = get(c_sol)
        if _norm(solution) in EMPTY_ANSWERS or USELESS.match(_norm(solution)) \
                or _norm(get(c_cat)) in USELESS_CATEGORIES:
            continue
        digits = re.sub(r"\D", "", get(c_id))
        if not digits:
            continue
        answers = [get(i) for i in form_cols]
        answers = [a for a in answers if _norm(a) not in EMPTY_ANSWERS]
        problem = _scrub(" ".join([get(c_desc), *answers]))[:400]
        created = get(c_created)[:10]
        if isinstance(row[c_created] if c_created is not None and c_created < len(row) else None, datetime):
            created = row[c_created].strftime("%Y-%m-%d")
        cases.append(Case(int(digits), _scrub(get(c_title))[:150], problem, _scrub(solution)[:200],
                          get(c_cat), get(c_lane), created))
    return cases


def cases_from_xlsx(path: str | Path) -> list[Case]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    cases = cases_from_rows(header, rows)
    wb.close()
    return cases


class CaseStore:
    """Файл базы случаев; если его перезаписали (новый импорт) — перечитывается сам, без перезапуска."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._base: CaseBase | None = None
        self._mtime: float | None = None

    def get(self) -> CaseBase | None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._base, self._mtime = None, None
            return None
        if mtime != self._mtime:
            try:
                self._base = CaseBase.load(self.path)
            except (ValueError, TypeError, KeyError) as exc:  # битый файл — работаем без подсказок
                import logging
                logging.getLogger(__name__).warning("База похожих случаев не прочиталась: %s", exc)
                self._base = None
            self._mtime = mtime
        return self._base

    def search(self, text: str, k: int = 3, exclude_id: int | None = None) -> list[Case]:
        base = self.get()
        return base.search(text, k=k, exclude_id=exclude_id) if base and k > 0 else []


def import_export(xlsx: str | Path, out: str | Path) -> list[str]:
    """python -m app.tools cases <файл.xlsx>: собрать базу похожих случаев из выгрузки Kaiten."""
    cases = cases_from_xlsx(xlsx)
    if not cases:
        return ["В выгрузке нет заявок с заполненной «Фактической категорией проблемы» — базу не менял."]
    CaseBase(cases).save(out)
    dates = sorted(c.created for c in cases if c.created)
    span = f" (с {dates[0]} по {dates[-1]})" if dates else ""
    return [f"Готово: {len(cases)} решённых заявок{span} → {out}",
            "Сервис подхватит базу сам, перезапускать не нужно."]

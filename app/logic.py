"""Чистая логика без сети: разбор вебхука, проверка назначения, SLA, контакты."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

RESPONSIBLE_TYPE = 2  # в Kaiten участник с type=2 — ответственный


def extract_card_id(payload: Any) -> int | None:
    """Достаёт id карточки из вебхука Kaiten, терпимо к формату.

    Порядок важен: у событий комментариев data.id — это id комментария,
    поэтому сначала смотрим card_id и data.card.id.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    card = data.get("card") if isinstance(data.get("card"), dict) else {}
    for candidate in (data.get("card_id"), card.get("id"), payload.get("card_id"), data.get("id")):
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int) and candidate > 0:
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


def is_responsible(card: dict, user_id: int) -> bool:
    for member in card.get("members") or []:
        if not isinstance(member, dict):
            continue
        member_id = member.get("id", member.get("user_id"))
        if member_id == user_id and member.get("type") == RESPONSIBLE_TYPE:
            return True
    return card.get("responsible_id") == user_id


def parse_kaiten_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute_deadline(card: dict, now: datetime, sla_minutes: int) -> datetime:
    """Срок карточки, если он в будущем, иначе now + SLA."""
    due = parse_kaiten_datetime(card.get("due_date"))
    if due and due > now:
        return due
    return now + timedelta(minutes=sla_minutes)


def warn_time(now: datetime, deadline: datetime, share: float) -> datetime:
    total = max((deadline - now).total_seconds(), 60.0)
    return now + timedelta(seconds=total * share)


def owner_id(card: dict) -> int | None:
    value = card.get("owner_id")
    if value is None and isinstance(card.get("owner"), dict):
        value = card["owner"].get("id")
    return value if isinstance(value, int) else None


def client_contact(card: dict, field_id: str) -> str:
    if not field_id:
        return ""
    props = card.get("properties") or {}
    value = props.get(field_id) if isinstance(props, dict) else None
    if isinstance(value, list):  # на случай поля-списка
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("value") or value.get("text") or value.get("name")
    return str(value).strip() if value not in (None, "") else ""


_TME = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:@)?([A-Za-z0-9_]{4,32})/?$", re.I)
_USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_PHONE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


@dataclass(frozen=True)
class Contact:
    kind: str  # "username" | "id" | "phone"
    value: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"


def normalize_contact(raw: str) -> Contact | None:
    """'@User', 't.me/User', '123456', '+996 555 123 456' -> Contact."""
    text = (raw or "").strip()
    if not text:
        return None
    match = _TME.match(text)
    if match:
        return Contact("username", match.group(1).lower())
    if re.fullmatch(r"-?\d{5,20}", text):
        return Contact("id", text)
    match = _USERNAME.match(text)
    if match:
        return Contact("username", match.group(1).lower())
    if _PHONE.match(text) and len(re.sub(r"\D", "", text)) >= 7:
        return Contact("phone", "+" + re.sub(r"\D", "", text))
    return None


_NO_TG = re.compile(r"ватсап|вотсап|whatsapp|нет\s+тг|нет\s+телеграм|без\s+тг|не\s+пользу", re.I)
_TME_ANY = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/@?([A-Za-z0-9_]{4,32})", re.I)
_AT_ANY = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})")
_PHONE_ANY = re.compile(r"(?<!\d)\+?\d[\d\s\-()]{8,16}\d(?!\d)")


def client_contact_from_text(text: str) -> str:
    """Контакт клиента из того, что куратор вписал в поле: «@Sara тг мамы», «89387005077 УЧИТЕЛЬ», ссылка t.me.

    Возвращает «@username» или «+79387005077»; пусто, если Telegram'а нет (только ватсап, «передам сам», «.»).
    """
    text = (text or "").strip()
    if not text:
        return ""
    match = _TME_ANY.search(text) or _AT_ANY.search(text)
    if match:
        return "@" + match.group(1)
    if _NO_TG.search(text):
        return ""  # «только ватсап», «нет тг у родителей» — в Telegram писать некуда
    if " " not in text and _USERNAME.match(text) and not text.isdigit():
        return "@" + text.lstrip("@")
    match = _PHONE_ANY.search(text)
    if match:
        digits = re.sub(r"\D", "", match.group(0))
        if 10 <= len(digits) <= 13:
            if len(digits) == 11 and digits.startswith("8"):
                digits = "7" + digits[1:]  # 8XXXXXXXXXX → +7XXXXXXXXXX
            return "+" + digits
    return ""


# --- автор заявки ---------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_AUTHOR_HINTS = ("author", "request", "service_desk", "sd_", "creator", "owner", "client", "customer")


def card_emails(card: dict) -> list[tuple[str, str]]:
    """Все e-mail в карточке как (путь, email), кроме e-mail участников карточки (это поддержка).

    Сначала идут найденные в полях, похожих на автора заявки Service Desk.
    """
    staff: set[str] = set()
    for member in card.get("members") or []:
        if isinstance(member, dict):
            staff.update(e.lower() for _, e in _walk_emails(member, "members"))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, email in _walk_emails(card, ""):
        low = email.lower()
        if path.startswith("members") or low in staff or low in seen:
            continue
        seen.add(low)
        found.append((path, low))
    found.sort(key=lambda pe: 0 if any(h in pe[0].lower() for h in _AUTHOR_HINTS) else 1)
    return found


def _walk_emails(node, path: str, depth: int = 0):
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_emails(value, f"{path}.{key}" if path else str(key), depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node[:200]):
            yield from _walk_emails(value, f"{path}[{index}]", depth + 1)
    elif isinstance(node, str) and "@" in node and len(node) < 2000:
        for match in _EMAIL.finditer(node):
            yield path, match.group(0)


def normalize_people(raw: dict) -> dict[str, str]:
    """employees.json: ключ — e-mail автора или id в Kaiten, значение — его Telegram."""
    people: dict[str, str] = {}
    for key, value in raw.items():
        key = str(key).strip()
        if key.startswith("_") or not str(value).strip():
            continue
        if key.isdigit():
            people[f"id:{key}"] = str(value).strip()
        elif _EMAIL.fullmatch(key):
            people[key.lower()] = str(value).strip()
    return people


def find_author(card: dict, people: dict[str, str], my_user_id: int) -> tuple[str | None, str | None, list[str]]:
    """(как нашли, Telegram автора, e-mail в карточке). Сначала по e-mail из заявки, потом по id владельца."""
    emails = [email for _, email in card_emails(card)]
    for email in emails:
        if email in people:
            return email, people[email], emails
    author_id = owner_id(card)
    if author_id is not None and author_id != my_user_id and f"id:{author_id}" in people:
        return f"id:{author_id}", people[f"id:{author_id}"], emails
    return None, None, emails


# --- колонки доски ------------------------------------------------------------

WAITING_NAMES = ("ждем ответа", "ждём ответа", "ожидаем ответа", "ждем ответ", "ждём ответ")


def column_title(col: dict) -> str:
    return str(col.get("title") or col.get("name") or "").strip()


def flatten_columns(board: dict) -> list[dict]:
    """Колонки доски, включая подколонки («Ждём ответа» внутри «В ожидании»)."""
    result = []
    for col in board.get("columns") or []:
        if isinstance(col, dict):
            result.append(col)
            for sub in col.get("subcolumns") or col.get("children") or []:
                if isinstance(sub, dict):
                    result.append(sub)
    return result


def waiting_columns(board: dict) -> list[int]:
    return [c["id"] for c in flatten_columns(board)
            if isinstance(c.get("id"), int) and column_title(c).casefold().replace("ё", "е")
            in {n.replace("ё", "е") for n in WAITING_NAMES}]


RECONTACT_NAMES = ("повторно связаться", "связаться повторно", "повторный контакт")


def recontact_columns(board: dict) -> list[int]:
    return [c["id"] for c in flatten_columns(board)
            if isinstance(c.get("id"), int) and column_title(c).casefold().replace("ё", "е") in RECONTACT_NAMES]


def moved_at(card: dict) -> datetime | None:
    """Когда карточку последний раз переносили между колонками (если Kaiten это отдаёт)."""
    for key in ("last_moved_at", "column_changed_at", "moved_at"):
        value = parse_kaiten_datetime(card.get(key))
        if value:
            return value
    return None


def is_urgent(card: dict) -> bool:
    """Срочная заявка: флаг «Срочно» (asap) в Kaiten."""
    return bool(card.get("asap"))

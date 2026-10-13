"""Настройки из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from .humanize import SPEED_KEYS


class ConfigError(ValueError):
    pass


def _get(env: dict, name: str, default: str | None = None, required: bool = False) -> str:
    value = env.get(name, default)
    if value is None or (required and str(value).strip() == ""):
        raise ConfigError(f"Не задана обязательная переменная {name}")
    return str(value).strip()


def _int(env: dict, name: str, default: str | None = None, required: bool = False) -> int:
    raw = _get(env, name, default, required)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должна быть целым числом, сейчас: {raw!r}") from exc


def _opt_int(env: dict, name: str) -> int | None:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должна быть целым числом, сейчас: {raw!r}") from exc


def _int_set(env: dict, name: str) -> frozenset[int]:
    raw = _get(env, name, required=True)
    try:
        values = frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: числа через запятую, сейчас: {raw!r}") from exc
    if not values:
        raise ConfigError(f"Не задана обязательная переменная {name}")
    return values


def _float(env: dict, name: str, default: str) -> float:
    raw = _get(env, name, default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должна быть числом, сейчас: {raw!r}") from exc


DEFAULT_CLIENT_LANES = "2-ой уровень,2-й уровень,второй уровень"


def _at_least(name: str, value: int, minimum: int = 1) -> int:
    if value < minimum:
        raise ConfigError(f"{name} должен быть не меньше {minimum}")
    return value


def _range_h(env: dict, name: str, default: str) -> tuple[float, float] | None:
    """'6-8' -> (6.0, 8.0) часов; '0' или пусто -> None (выключено)."""
    raw = _get(env, name, default).replace(",", ".")
    if raw in {"", "0"}:
        return None
    try:
        parts = [float(x) for x in raw.split("-")]
    except ValueError as exc:
        raise ConfigError(f"{name}: например 6-8 (часы) или 0, чтобы выключить; сейчас: {raw!r}") from exc
    low, high = (parts[0], parts[0]) if len(parts) == 1 else (parts[0], parts[1])
    if len(parts) > 2 or low <= 0 or high < low:
        raise ConfigError(f"{name}: например 6-8 (часы) или 0, чтобы выключить; сейчас: {raw!r}")
    return low, high


def _opt_int_set(env: dict, name: str) -> frozenset[int]:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return frozenset()
    return _int_set(env, name)


def _reaction_mode(env: dict) -> str:
    raw = _get(env, "REACTION_MODE", "classic").lower()
    aliases = {"classic": "classic", "old": "classic", "как раньше": "classic",
               "assess": "assess", "оценка": "assess", "new": "assess"}
    if raw not in aliases:
        raise ConfigError(f"REACTION_MODE: classic (как раньше) или assess (оценка), сейчас: {raw!r}")
    return aliases[raw]


def _eta(env: dict) -> tuple[int, tuple]:
    """ASSESS_ETA_MIN: «40» или «Камера/Микрофон=60; Bitrix24=120; *=40» — минуты по категории."""
    raw = _get(env, "ASSESS_ETA_MIN", "40")
    default, pairs = 40, []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.rpartition("=")
        try:
            minutes = int(value.strip())
        except ValueError as exc:
            raise ConfigError(f"ASSESS_ETA_MIN: минуты числом, например «40» или «Bitrix24=120; *=40», "
                              f"сейчас: {raw!r}") from exc
        if minutes <= 0:
            raise ConfigError(f"ASSESS_ETA_MIN: минуты больше нуля, сейчас: {raw!r}")
        if not sep or name.strip() in {"", "*"}:
            default = minutes
        else:
            pairs.append((name.strip().casefold(), minutes))
    return default, tuple(pairs)


def _bool(env: dict, name: str, default: str = "false") -> bool:
    return _get(env, name, default).lower() in {"1", "true", "yes", "да", "on"}


def _hours(raw: str) -> tuple[time, time]:
    try:
        start_s, end_s = raw.split("-")
        start = time.fromisoformat(start_s.strip())
        end = time.fromisoformat(end_s.strip())
    except ValueError as exc:
        raise ConfigError(f"WORK_HOURS в формате 09:00-18:00, сейчас: {raw!r}") from exc
    if start >= end:
        raise ConfigError("WORK_HOURS: начало должно быть раньше конца")
    return start, end


def _days(raw: str) -> frozenset[int]:
    """'1-5' или '1,2,3,4,5,6' -> номера дней ISO (1 = понедельник, 7 = воскресенье)."""
    days: set[int] = set()
    try:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = (int(x) for x in part.split("-"))
                days.update(range(a, b + 1))
            else:
                days.add(int(part))
    except ValueError as exc:
        raise ConfigError(f"WORK_DAYS: например 1-5 или 1,2,3,4,5,6; сейчас: {raw!r}") from exc
    if not days or not days <= set(range(1, 8)):
        raise ConfigError(f"WORK_DAYS: дни 1..7, сейчас: {raw!r}")
    return frozenset(days)


@dataclass
class Config:
    # Kaiten
    kaiten_base_url: str
    kaiten_token: str
    card_url_prefix: str
    my_user_id: int
    in_work_column_id: int
    in_work_board_id: int | None
    queue_column_ids: frozenset[int]
    waiting_column_ids: frozenset[int]
    recontact_column_ids: frozenset[int]
    author_tg_field: str
    client_tg_field: str
    client_lanes: tuple[str, ...]
    # SLA
    sla_minutes: int
    sla_warn_share: float
    # Повторное сообщение «проблема актуальна?», если в чате тишина (часы, случайно в диапазоне)
    followup_after_h: tuple[float, float] | None
    # Сколько часов держать приветствие, пока автор впервые не напишет на аккаунт
    hold_unknown_chat_h: float
    # Не здороваться второй раз по той же заявке раньше, чем через столько часов
    regreet_after_h: float
    specialist_name: str
    auto_to_work: bool
    wait_hint_min: int
    heartbeat: bool
    log_file: str
    # Детектор инцидентов (вся доска, для старшего смены и руководителя)
    incidents: bool
    manager_chat_id: str
    incident_window_min: int
    incident_same_topic: int
    incident_spike: int
    incident_by_text: bool    # массовый сбой — только похожие заявки, а не просто одна тема
    untaken_alert_min: int
    untaken_manager_min: int
    backlog_limit: int
    digest: bool
    digest_by_person: bool
    report_weekly: bool
    report_monthly: bool
    dashboard_url: str        # ссылка на дашборд SLA/KPI — в отчётах бота рядом с цифрами команды
    # Похожие решённые заявки
    cases_file: str
    fact_category_field: str
    fact_solution_field: str
    similar_hints: int
    # Транспорт сообщений от корпоративного аккаунта
    transport: str  # "business" | "userbot"
    telegram_api_base: str
    business_bot_token: str
    userbot_api_id: int | None
    userbot_api_hash: str
    userbot_session: str
    # Служебные уведомления лично тебе
    alert_bot_token: str
    alert_chat_id: str
    # «Человечность»
    tz: ZoneInfo
    work_start: time
    work_end: time
    work_days: frozenset[int]
    delay_min_s: int
    delay_max_s: int
    typing_cps_min: float
    typing_cps_max: float
    take_outside_hours: bool  # false — до/после WORK_HOURS заявки сами не берём, только уведомление
    speed: str  # instant | fast | slow | custom (custom — HUMAN_DELAY_* и TYPING_CPS_*); /speed меняет на лету
    # Прочее
    webhook_secret: str
    poll_interval_s: int
    db_path: str
    employees_file: str
    messages_file: str
    host: str
    port: int
    dry_run: bool
    # Время реакции = когда специалист реально взял заявку в работу (решение созвона 09.10.2026).
    # classic — как раньше: бот сам переносит в «В работе»; assess — бот здоровается, а карточка
    # остаётся в «Новой», пока ты не оценишь заявку и не нажмёшь «В работу»
    reaction_mode: str = "classic"
    assess_wait_column_ids: frozenset = frozenset()   # «Ожидание информации для оценки»
    assess_eta_default_min: int = 40                  # «ориентировочное время решения» для клиента
    assess_eta_by_category: tuple = ()                # ((категория в нижнем регистре, минуты), …)
    assess_require_category: bool = True              # без фактической категории в работу не переводить
    assess_remind_min: int = 4                        # напомнить, если карточка в «Новой» дольше
    fields_in_bot: bool = True                        # заполнять фактические поля кнопками в боте
    team_file: str = "config/team.json"               # коллеги на этом же боте
    corp_prefill: bool = True                         # готовые тексты — ещё и в чат корпоративного аккаунта с ботом

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        transport = _get(env, "TRANSPORT", "business").lower()
        if transport not in {"business", "userbot"}:
            raise ConfigError("TRANSPORT должен быть business или userbot")
        business_token = _get(env, "BUSINESS_BOT_TOKEN", "", required=transport == "business")
        api_id = _opt_int(env, "USERBOT_API_ID")
        api_hash = _get(env, "USERBOT_API_HASH", "")
        if transport == "userbot" and (not api_id or not api_hash):
            raise ConfigError("Для TRANSPORT=userbot нужны USERBOT_API_ID и USERBOT_API_HASH")
        alert_token = _get(env, "ALERT_BOT_TOKEN", "") or business_token
        if not alert_token:
            raise ConfigError("Нужен ALERT_BOT_TOKEN (бот, который пишет лично тебе)")
        work_start, work_end = _hours(_get(env, "WORK_HOURS", "11:30-20:30"))
        delay_min = _int(env, "HUMAN_DELAY_MIN_S", "40")
        delay_max = _int(env, "HUMAN_DELAY_MAX_S", "150")
        if delay_min < 0 or delay_max < delay_min:
            raise ConfigError("Нужно 0 <= HUMAN_DELAY_MIN_S <= HUMAN_DELAY_MAX_S")
        cps_min = _float(env, "TYPING_CPS_MIN", "4")
        cps_max = _float(env, "TYPING_CPS_MAX", "7")
        if cps_min <= 0 or cps_max < cps_min:
            raise ConfigError("Нужно 0 < TYPING_CPS_MIN <= TYPING_CPS_MAX")
        speed = _get(env, "SPEED", "fast").lower()
        if speed not in SPEED_KEYS:
            raise ConfigError("SPEED должен быть одним из: " + ", ".join(SPEED_KEYS))
        warn_share = _float(env, "SLA_WARN_SHARE", "0.8")
        if not 0 < warn_share < 1:
            raise ConfigError("SLA_WARN_SHARE должен быть между 0 и 1, например 0.8")
        poll_interval = _int(env, "POLL_INTERVAL_S", "30")
        webhook_secret = _get(env, "WEBHOOK_SECRET", "")
        if poll_interval < 0:
            raise ConfigError("POLL_INTERVAL_S не может быть отрицательным")
        if 0 < poll_interval < 10:
            raise ConfigError("POLL_INTERVAL_S: не чаще раза в 10 секунд, чтобы не нагружать Kaiten")
        if poll_interval == 0 and not webhook_secret:
            raise ConfigError("Нужно либо POLL_INTERVAL_S > 0 (опрос Kaiten), либо WEBHOOK_SECRET (вебхук)")
        if webhook_secret and len(webhook_secret) < 12:
            raise ConfigError("WEBHOOK_SECRET должен быть длиной от 12 символов")
        sla_minutes = _int(env, "SLA_MINUTES", "240")
        if sla_minutes <= 0:
            raise ConfigError("SLA_MINUTES должен быть больше 0")
        return cls(
            kaiten_base_url=_get(env, "KAITEN_BASE_URL", required=True).rstrip("/"),
            kaiten_token=_get(env, "KAITEN_TOKEN", required=True),
            card_url_prefix=_get(env, "KAITEN_CARD_URL_PREFIX", required=True),
            my_user_id=_int(env, "MY_KAITEN_USER_ID", required=True),
            in_work_column_id=_int(env, "IN_WORK_COLUMN_ID", required=True),
            in_work_board_id=_opt_int(env, "IN_WORK_BOARD_ID"),
            queue_column_ids=_int_set(env, "QUEUE_COLUMN_IDS"),
            waiting_column_ids=_opt_int_set(env, "WAITING_COLUMN_IDS"),
            recontact_column_ids=_opt_int_set(env, "RECONTACT_COLUMN_IDS"),
            author_tg_field=_get(env, "AUTHOR_TG_FIELD", "Ваш телеграм"),
            client_tg_field=_get(env, "CLIENT_TG_FIELD", "") or (
                "Telegram ученика/учителя" if _get(env, "CLIENT_LANES", DEFAULT_CLIENT_LANES) else ""),
            client_lanes=tuple(x.strip().casefold() for x in _get(env, "CLIENT_LANES", DEFAULT_CLIENT_LANES).split(",")
                               if x.strip()),
            sla_minutes=sla_minutes,
            sla_warn_share=warn_share,
            followup_after_h=_range_h(env, "FOLLOWUP_AFTER_H", "6-8"),
            hold_unknown_chat_h=_float(env, "HOLD_UNKNOWN_CHAT_H", "24"),
            regreet_after_h=_float(env, "REGREET_AFTER_H", "12"),
            specialist_name=_get(env, "SPECIALIST_NAME", ""),
            auto_to_work=_bool(env, "AUTO_TO_WORK", "true"),
            wait_hint_min=_int(env, "WAIT_HINT_MIN", "30"),
            heartbeat=_bool(env, "HEARTBEAT", "true"),
            log_file=_get(env, "LOG_FILE", "data/relay.log"),
            incidents=_bool(env, "INCIDENTS", "true"),
            manager_chat_id=_get(env, "MANAGER_CHAT_ID", ""),
            incident_window_min=_at_least("INCIDENT_WINDOW_MIN", _int(env, "INCIDENT_WINDOW_MIN", "30")),
            incident_same_topic=_at_least("INCIDENT_SAME_TOPIC", _int(env, "INCIDENT_SAME_TOPIC", "3"), 2),
            incident_spike=_at_least("INCIDENT_SPIKE", _int(env, "INCIDENT_SPIKE", "8"), 2),
            incident_by_text=_bool(env, "INCIDENT_BY_TEXT", "true"),
            untaken_alert_min=_int(env, "UNTAKEN_ALERT_MIN", "5"),
            untaken_manager_min=_int(env, "UNTAKEN_MANAGER_MIN", "0"),
            backlog_limit=_int(env, "BACKLOG_LIMIT", "5"),
            digest=_bool(env, "DIGEST", "true"),
            digest_by_person=_bool(env, "DIGEST_BY_PERSON", "true"),
            report_weekly=_bool(env, "REPORT_WEEKLY", "true"),
            report_monthly=_bool(env, "REPORT_MONTHLY", "true"),
            dashboard_url=_get(env, "DASHBOARD_URL", "").strip(),
            cases_file=_get(env, "CASES_FILE", "data/cases.json"),
            similar_hints=_int(env, "SIMILAR_HINTS", "3"),
            fact_category_field=_get(env, "FACT_CATEGORY_FIELD", ""),
            fact_solution_field=_get(env, "FACT_SOLUTION_FIELD", ""),
            transport=transport,
            telegram_api_base=_get(env, "TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            business_bot_token=business_token,
            userbot_api_id=api_id,
            userbot_api_hash=api_hash,
            userbot_session=_get(env, "USERBOT_SESSION", "data/corp_account"),
            alert_bot_token=alert_token,
            alert_chat_id=_get(env, "ALERT_CHAT_ID", required=True),
            tz=ZoneInfo(_get(env, "TZ_NAME", "Asia/Bishkek")),
            work_start=work_start,
            work_end=work_end,
            work_days=_days(_get(env, "WORK_DAYS", "1-7")),
            delay_min_s=delay_min,
            delay_max_s=delay_max,
            typing_cps_min=cps_min,
            typing_cps_max=cps_max,
            take_outside_hours=_bool(env, "TAKE_OUTSIDE_HOURS", "false"),
            speed=speed,
            webhook_secret=webhook_secret,
            poll_interval_s=poll_interval,
            db_path=_get(env, "DB_PATH", "data/state.db"),
            employees_file=_get(env, "EMPLOYEES_FILE", "config/employees.json"),
            messages_file=_get(env, "MESSAGES_FILE", "config/messages.json"),
            host=_get(env, "HOST", "0.0.0.0"),
            port=_int(env, "PORT", "8080"),
            dry_run=_bool(env, "DRY_RUN", "false"),
            reaction_mode=_reaction_mode(env),
            assess_wait_column_ids=_opt_int_set(env, "ASSESS_WAIT_COLUMN_IDS"),
            assess_eta_default_min=_eta(env)[0],
            assess_eta_by_category=_eta(env)[1],
            assess_require_category=_bool(env, "ASSESS_REQUIRE_CATEGORY", "true"),
            assess_remind_min=_int(env, "ASSESS_REMIND_MIN", "4"),
            fields_in_bot=_bool(env, "FIELDS_IN_BOT", "true"),
            team_file=_get(env, "TEAM_FILE", "config/team.json"),
            corp_prefill=_bool(env, "CORP_PREFILL", "true"),
        )

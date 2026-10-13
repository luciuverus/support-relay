import pytest

from app.config import ConfigError
from tests.fakes import make_cfg


def test_defaults_and_alert_token_fallback():
    cfg = make_cfg()
    assert cfg.transport == "business" and cfg.alert_bot_token == "BIZ"
    assert cfg.in_work_board_id is None and cfg.dry_run is False


@pytest.mark.parametrize("overrides", [
    {"TRANSPORT": "sms"},
    {"TRANSPORT": "userbot"},  # без api_id/hash
    {"WORK_HOURS": "18:00-09:00"},
    {"WORK_DAYS": "0-9"},
    {"SLA_WARN_SHARE": "1.5"},
    {"HUMAN_DELAY_MIN_S": "100", "HUMAN_DELAY_MAX_S": "10"},
    {"MY_KAITEN_USER_ID": "abc"},
    {"QUEUE_COLUMN_IDS": ""},
    {"POLL_INTERVAL_S": "0", "WEBHOOK_SECRET": ""},
    {"POLL_INTERVAL_S": "5"},
    {"POLL_INTERVAL_S": "-1"},
    {"WEBHOOK_SECRET": "short"},
    {"QUEUE_COLUMN_IDS": "очередь"},
])
def test_invalid_config_rejected(overrides):
    with pytest.raises(ConfigError):
        make_cfg(**overrides)


def test_userbot_config_ok():
    cfg = make_cfg(TRANSPORT="userbot", BUSINESS_BOT_TOKEN="", ALERT_BOT_TOKEN="A",
                   USERBOT_API_ID="123", USERBOT_API_HASH="h")
    assert cfg.transport == "userbot" and cfg.alert_bot_token == "A"


def test_polling_without_webhook_is_valid_default():
    cfg = make_cfg(WEBHOOK_SECRET="")
    assert cfg.poll_interval_s == 30 and cfg.webhook_secret == ""

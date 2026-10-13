from datetime import datetime, timedelta, timezone

from app.logic import (client_contact, compute_deadline, extract_card_id, is_responsible,
                       normalize_contact, owner_id, warn_time)

NOW = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)


def test_extract_card_id_formats():
    assert extract_card_id({"data": {"card_id": 15, "id": 999}}) == 15  # комментарий: id != карточка
    assert extract_card_id({"data": {"card": {"id": 16}}}) == 16
    assert extract_card_id({"data": {"id": 17}}) == 17
    assert extract_card_id({"card_id": "18"}) == 18
    assert extract_card_id({"data": {"id": True}}) is None
    assert extract_card_id({}) is None
    assert extract_card_id(["x"]) is None


def test_is_responsible():
    assert is_responsible({"members": [{"id": 7, "type": 2}]}, 7)
    assert is_responsible({"members": [{"user_id": 7, "type": 2}]}, 7)
    assert not is_responsible({"members": [{"id": 7, "type": 1}]}, 7)  # просто участник
    assert not is_responsible({"members": [{"id": 8, "type": 2}]}, 7)
    assert is_responsible({"members": [], "responsible_id": 7}, 7)


def test_deadline_prefers_future_due_date_else_sla():
    due = (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert compute_deadline({"due_date": due}, NOW, 240) == NOW + timedelta(hours=2)
    past = (NOW - timedelta(hours=1)).isoformat()
    assert compute_deadline({"due_date": past}, NOW, 240) == NOW + timedelta(minutes=240)
    assert compute_deadline({"due_date": "garbage"}, NOW, 30) == NOW + timedelta(minutes=30)
    assert compute_deadline({}, NOW, 60) == NOW + timedelta(minutes=60)


def test_warn_time_share():
    assert warn_time(NOW, NOW + timedelta(minutes=100), 0.8) == NOW + timedelta(minutes=80)


def test_owner_and_client_contact():
    assert owner_id({"owner_id": 5}) == 5
    assert owner_id({"owner": {"id": 6}}) == 6
    assert owner_id({}) is None
    assert client_contact({"properties": {"id_1": " @abc_def "}}, "id_1") == "@abc_def"
    assert client_contact({"properties": {"id_1": ["@abc_def"]}}, "id_1") == "@abc_def"
    assert client_contact({"properties": {"id_1": {"value": "@abc_def"}}}, "id_1") == "@abc_def"
    assert client_contact({"properties": {}}, "id_1") == ""
    assert client_contact({"properties": {"id_1": "@x"}}, "") == ""


def test_normalize_contact():
    assert normalize_contact("@User_Name").key == "username:user_name"
    assert normalize_contact("https://t.me/User_Name").key == "username:user_name"
    assert normalize_contact("t.me/@User_Name").key == "username:user_name"
    assert normalize_contact("User_Name").key == "username:user_name"
    assert normalize_contact("123456789").key == "id:123456789"
    assert normalize_contact("+996 (555) 12-34-56").key == "phone:+996555123456"
    assert normalize_contact("") is None
    assert normalize_contact("не знаю") is None

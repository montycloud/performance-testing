import pytest

from mcperf.core.credentials import UserClaimer, load_env_credentials, load_users_csv, password_for


def test_load_users_csv_respects_limit(tmp_path):
    path = tmp_path / "users.csv"
    path.write_text("Email,Password\na@x.test,1\nb@x.test,2\nc@x.test,3\n", encoding="utf-8")
    assert [u["Email"] for u in load_users_csv(path, 2)] == ["a@x.test", "b@x.test"]


def test_load_users_csv_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_users_csv(tmp_path / "missing.csv", 1)
    empty = tmp_path / "empty.csv"
    empty.write_text("Email,Password\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_users_csv(empty, 1)


def test_claimer_is_round_robin():
    claimer = UserClaimer([{"Email": "a"}, {"Email": "b"}])
    assert [claimer.claim()["Email"] for _ in range(5)] == ["a", "b", "a", "b", "a"]


def test_password_prefers_new_password():
    assert password_for({"Password": "old", "New Password": "new"}) == "new"
    assert password_for({"Password": "old", "New Password": ""}) == "old"


def test_env_credentials(monkeypatch):
    monkeypatch.setenv("T_ACCESS_KEY_2", "ak2")
    monkeypatch.setenv("T_SECRET_KEY_2", "sk2")
    monkeypatch.setenv("T_ACCESS_KEY_1", "ak1")
    monkeypatch.setenv("T_SECRET_KEY_1", "sk1")
    monkeypatch.setenv("T_ACCESS_KEY_3", "ak3")  # no secret → skipped
    assert load_env_credentials("T") == [
        {"AccessKey": "ak1", "SecretKey": "sk1"},
        {"AccessKey": "ak2", "SecretKey": "sk2"},
    ]


def test_env_credentials_none_found():
    with pytest.raises(ValueError, match="NOPE_ACCESS_KEY_1"):
        load_env_credentials("NOPE")

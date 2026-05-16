import pytest

from hermes_plugin_xmpp.config import (
    DEFAULT_MAM_CATCHUP_LIMIT,
    DEFAULT_MAM_REPLAY_GRACE_SECONDS,
    DEFAULT_PORT,
    is_configured,
    load_config,
    validate,
)

_XMPP_ENV_VARS = (
    "XMPP_JID", "XMPP_PASSWORD", "XMPP_SERVER", "XMPP_PORT", "XMPP_USE_TLS",
    "XMPP_RESOURCE", "XMPP_MUC_ROOMS", "XMPP_MUC_NICKNAME",
    "XMPP_ALLOWED_JIDS", "XMPP_ALLOW_ALL_USERS", "XMPP_HOME_JID",
    "XMPP_MAM_REPLAY_GRACE_SECONDS", "XMPP_MAM_CATCHUP_LIMIT",
)


def _clear_xmpp_env(monkeypatch):
    for key in _XMPP_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def test_env_only(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    monkeypatch.setenv("XMPP_JID", "hermes@example.com")
    monkeypatch.setenv("XMPP_PASSWORD", "secret")
    monkeypatch.setenv("XMPP_ALLOWED_JIDS", "me@example.com, ME@Example.com")

    cfg = load_config()
    assert cfg.bare_jid == "hermes@example.com"
    assert cfg.port == DEFAULT_PORT
    assert cfg.use_tls is True
    assert cfg.allowed_jids == {"me@example.com"}
    assert cfg.home_jid == "me@example.com"  # derived from allowed
    assert cfg.mam_replay_grace_seconds == DEFAULT_MAM_REPLAY_GRACE_SECONDS
    assert cfg.mam_catchup_limit == DEFAULT_MAM_CATCHUP_LIMIT


def test_env_beats_yaml(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    monkeypatch.setenv("XMPP_JID", "env-jid@example.com")
    monkeypatch.setenv("XMPP_PASSWORD", "env-pw")
    monkeypatch.setenv("XMPP_PORT", "5223")

    cfg = load_config({"jid": "yaml-jid@example.com", "password": "yaml-pw",
                       "port": 5222, "server": "yaml.example.com"})
    assert cfg.bare_jid == "env-jid@example.com"
    assert cfg.port == 5223
    assert cfg.server == "yaml.example.com"  # falls through, env not set


def test_yaml_fallback(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    cfg = load_config({
        "jid": "bot@example.com",
        "password": "pw",
        "muc_rooms": ["team@conference.example.com", "ops@conference.example.com"],
        "muc_nickname": "hermes",
        "allow_all_users": True,
    })
    assert cfg.muc_rooms == ["team@conference.example.com", "ops@conference.example.com"]
    assert cfg.muc_nickname == "hermes"
    assert cfg.allow_all_users is True
    assert cfg.allowed_jids == set()
    assert cfg.home_jid == ""


def test_missing_jid_raises(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    with pytest.raises(ValueError):
        load_config({"password": "pw"})


def test_is_configured(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    assert is_configured() is False
    assert is_configured({"jid": "a@b", "password": "p"}) is True
    monkeypatch.setenv("XMPP_JID", "a@b")
    monkeypatch.setenv("XMPP_PASSWORD", "p")
    assert is_configured() is True


def test_validate_reports_bad_muc(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    errors = validate({
        "jid": "bot@example.com",
        "password": "pw",
        "muc_rooms": ["good@conf.example.com", "not-a-jid"],
    })
    assert any("not-a-jid" in e for e in errors)


def test_validate_clean(monkeypatch):
    _clear_xmpp_env(monkeypatch)
    errors = validate({"jid": "bot@example.com", "password": "pw"})
    assert errors == []

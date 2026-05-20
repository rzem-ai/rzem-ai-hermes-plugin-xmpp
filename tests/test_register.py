"""Verify the directory-plugin ``__init__.py`` registers itself in the
shape Hermes's plugin loader expects (``ctx.register_platform`` kwargs).
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_outer_shim():
    """The outer __init__.py lives at the plugin root, not inside the
    importable ``hermes_plugin_xmpp`` package — load it by path."""
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "xmpp_platform_outer_shim", root / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_register_calls_register_platform_with_expected_kwargs():
    shim = _load_outer_shim()

    captured: dict = {}

    def fake_register_platform(**kwargs):
        captured.update(kwargs)

    ctx = SimpleNamespace(register_platform=fake_register_platform)
    shim.register(ctx)

    assert captured["name"] == "xmpp"
    assert captured["label"] == "XMPP"
    assert callable(captured["adapter_factory"])
    assert callable(captured["check_fn"])
    assert callable(captured["validate_config"])
    assert callable(captured["env_enablement_fn"])
    assert callable(captured["standalone_sender_fn"])
    assert captured["required_env"] == ["XMPP_JID", "XMPP_PASSWORD"]
    assert captured["allowed_users_env"] == "XMPP_ALLOWED_JIDS"
    assert captured["allow_all_env"] == "XMPP_ALLOW_ALL_USERS"
    assert captured["cron_deliver_env_var"] == "XMPP_HOME_JID"
    assert captured["platform_hint"]  # non-empty


def test_validate_config_wrapper_flips_polarity():
    shim = _load_outer_shim()

    captured: dict = {}

    def fake_register_platform(**kwargs):
        captured.update(kwargs)

    shim.register(SimpleNamespace(register_platform=fake_register_platform))
    validate_config = captured["validate_config"]

    good = SimpleNamespace(extra={"jid": "bot@example.com", "password": "pw"})
    bad = SimpleNamespace(extra={"jid": "bot@example.com", "password": "pw",
                                 "muc_rooms": ["not-a-jid"]})
    assert validate_config(good) is True
    assert validate_config(bad) is False


def test_env_enablement_fn_returns_none_without_creds(monkeypatch):
    shim = _load_outer_shim()
    for k in ("XMPP_JID", "XMPP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert shim._env_enablement_fn() is None


def test_env_enablement_fn_seeds_from_env(monkeypatch):
    shim = _load_outer_shim()
    monkeypatch.setenv("XMPP_JID", "bot@example.com")
    monkeypatch.setenv("XMPP_PASSWORD", "pw")
    monkeypatch.setenv("XMPP_ALLOWED_JIDS", "me@example.com,you@example.com")
    monkeypatch.setenv("XMPP_HOME_JID", "me@example.com")
    seed = shim._env_enablement_fn()
    assert seed["jid"] == "bot@example.com"
    assert seed["password"] == "pw"
    assert seed["allowed_jids"] == ["me@example.com", "you@example.com"]
    assert seed["home_channel"] == {"chat_id": "me@example.com",
                                    "name": "me@example.com"}

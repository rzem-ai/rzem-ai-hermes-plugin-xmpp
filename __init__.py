"""Hermes platform-plugin shim for ``hermes-plugin-xmpp``.

Discovery contract — Hermes's directory plugin loader (see
``hermes_cli/plugins.py:_load_directory_module``) imports
``$HERMES_HOME/plugins/<dir>/__init__.py`` as
``hermes_plugins.<slug>`` and calls its top-level :func:`register`
with a ``PluginContext``. The context's :meth:`register_platform`
forwards into ``gateway.platform_registry``, whose runtime expects:

* ``check_fn()                  -> bool``                      (no args)
* ``validate_config(PlatformConfig)  -> bool``                 (truthy = valid)
* ``adapter_factory(PlatformConfig)  -> BasePlatformAdapter``
* ``env_enablement_fn()         -> dict | None``               (env → extra seed)

The inner ``hermes_plugin_xmpp.adapter.register`` was written for a
different shape: it ignores its ``ctx`` argument, returns a
descriptor dict, and the descriptor's callables take an extras
``Mapping`` (not the full ``PlatformConfig``). ``validate`` also
returns a list of error strings — opposite polarity from the bool
Hermes expects. This shim:

* calls the inner register to harvest the descriptor;
* wraps ``validate`` to extract ``PlatformConfig.extra`` and flip
  its return-value polarity (empty errors list → ``True``);
* installs a Hermes-shaped ``env_enablement_fn`` (the inner
  ``_env_enable_hook`` takes ``(env, yaml_extra)`` and mutates env —
  the opposite direction of what Hermes wants here, so it's dropped);
* adds the Hermes-side metadata the inner descriptor doesn't surface
  (``required_env``, ``allowed_users_env``, ``allow_all_env``,
  ``cron_deliver_env_var``, ``install_hint``, ``emoji``).
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from hermes_plugin_xmpp.adapter import register as _inner_register


def _platform_config_extra(config: Any) -> dict:
    """Extract ``.extra`` from a PlatformConfig, defaulting to an empty dict."""
    if config is None:
        return {}
    if isinstance(config, Mapping):
        return dict(config)
    return getattr(config, "extra", None) or {}


def _wrap_validate(inner_validate):
    """Adapt inner ``validate(extra) -> list[str]`` to ``(PlatformConfig) -> bool``."""
    def wrapper(config: Any) -> bool:
        errors = inner_validate(_platform_config_extra(config))
        return not errors

    return wrapper


def _env_enablement_fn() -> dict | None:
    """Seed ``PlatformConfig.extra`` from XMPP_* env vars.

    Called by the platform registry's env-enablement hook BEFORE the
    adapter is constructed, so ``gateway status`` can reflect env-only
    setups without instantiating slixmpp. Returns ``None`` when XMPP
    isn't minimally configured (missing JID or password).

    ``home_channel`` is interpreted by the core hook as a HomeChannel
    dataclass, not merged into ``extra``.
    """
    jid = os.getenv("XMPP_JID", "").strip()
    password = os.getenv("XMPP_PASSWORD", "").strip()
    if not (jid and password):
        return None

    seed: dict[str, Any] = {"jid": jid, "password": password}

    for env_key, extra_key in (
        ("XMPP_SERVER", "server"),
        ("XMPP_RESOURCE", "resource"),
        ("XMPP_MUC_NICKNAME", "muc_nickname"),
    ):
        val = os.getenv(env_key, "").strip()
        if val:
            seed[extra_key] = val

    port = os.getenv("XMPP_PORT", "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass

    for env_key, extra_key in (
        ("XMPP_USE_TLS", "use_tls"),
        ("XMPP_ALLOW_ALL_USERS", "allow_all_users"),
    ):
        val = os.getenv(env_key, "").strip().lower()
        if val:
            seed[extra_key] = val in ("1", "true", "yes", "on", "y")

    for env_key, extra_key in (
        ("XMPP_MUC_ROOMS", "muc_rooms"),
        ("XMPP_ALLOWED_JIDS", "allowed_jids"),
    ):
        val = os.getenv(env_key, "").strip()
        if val:
            seed[extra_key] = [p.strip() for p in val.split(",") if p.strip()]

    home = os.getenv("XMPP_HOME_JID", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("XMPP_HOME_NAME", home),
        }

    return seed


_DESCRIPTOR_TO_KWARG = {
    "name": "name",
    "label": "label",
    "factory": "adapter_factory",
    "is_configured": "check_fn",
    "validate": "validate_config",
    "interactive_setup": "setup_fn",
    "standalone_sender": "standalone_sender_fn",
    "platform_hint": "platform_hint",
}


def register(ctx: Any) -> None:
    """Hermes plugin entry point — bridges the inner dict-returning register."""
    descriptor = _inner_register(ctx) or {}
    kwargs: dict[str, Any] = {
        target: descriptor[src]
        for src, target in _DESCRIPTOR_TO_KWARG.items()
        if src in descriptor
    }

    if "validate_config" in kwargs:
        kwargs["validate_config"] = _wrap_validate(kwargs["validate_config"])

    kwargs.setdefault("env_enablement_fn", _env_enablement_fn)
    kwargs.setdefault("required_env", ["XMPP_JID", "XMPP_PASSWORD"])
    kwargs.setdefault("install_hint", "uv pip install --python <hermes-venv>/bin/python slixmpp aiohttp")
    kwargs.setdefault("allowed_users_env", "XMPP_ALLOWED_JIDS")
    kwargs.setdefault("allow_all_env", "XMPP_ALLOW_ALL_USERS")
    kwargs.setdefault("cron_deliver_env_var", "XMPP_HOME_JID")
    kwargs.setdefault("emoji", "💬")

    ctx.register_platform(**kwargs)


__all__ = ["register"]

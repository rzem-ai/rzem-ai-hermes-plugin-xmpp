"""Hermes directory-plugin entry point for ``hermes-plugin-xmpp``.

Hermes's directory loader (``hermes_cli/plugins.py:_load_directory_module``)
imports ``$HERMES_HOME/plugins/<dir>/__init__.py`` as
``hermes_plugins.<slug>`` and calls its top-level :func:`register`
with a ``PluginContext``. The context's :meth:`register_platform`
forwards into ``gateway.platform_registry`` and accepts a flat kwargs
shape (see ``gateway.platform_registry.PlatformEntry``).

This shim just translates module-level callables into that flat shape
and seeds ``PlatformConfig.extra`` from XMPP_* env vars so
``gateway status`` reflects env-only setups without instantiating the
adapter.
"""

from __future__ import annotations

import os
from typing import Any

from adapter import (
    PLATFORM,
    PLATFORM_HINT,
    adapter_factory,
    interactive_setup,
    standalone_sender_fn,
    validate,
)


def _validate_config(config: Any) -> bool:
    """``validate`` returns an error list; the registry wants a bool."""
    extra = getattr(config, "extra", None) or {}
    return not validate(extra)


def _check_fn() -> bool:
    """Plugin is "available" when slixmpp is importable."""
    try:
        import slixmpp  # noqa: F401
    except Exception:
        return False
    return True


def _env_enablement_fn() -> dict | None:
    """Seed ``PlatformConfig.extra`` from XMPP_* env vars.

    Called BEFORE the adapter is constructed so ``gateway status`` reflects
    env-only configuration. Returns ``None`` when the minimum (JID +
    password) is missing.
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


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_platform(
        name=PLATFORM,
        label="XMPP",
        adapter_factory=adapter_factory,
        check_fn=_check_fn,
        validate_config=_validate_config,
        required_env=["XMPP_JID", "XMPP_PASSWORD"],
        install_hint="uv pip install --python <hermes-venv>/bin/python slixmpp aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement_fn,
        standalone_sender_fn=standalone_sender_fn,
        allowed_users_env="XMPP_ALLOWED_JIDS",
        allow_all_env="XMPP_ALLOW_ALL_USERS",
        cron_deliver_env_var="XMPP_HOME_JID",
        platform_hint=PLATFORM_HINT,
        emoji="💬",
    )


__all__ = ["register"]

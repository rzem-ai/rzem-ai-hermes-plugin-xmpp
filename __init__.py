"""Hermes platform-plugin shim for ``hermes-plugin-xmpp``.

Discovery contract — Hermes's directory plugin loader (see
``hermes_cli/plugins.py:_load_directory_module``) imports
``$HERMES_HOME/plugins/<dir>/__init__.py`` as
``hermes_plugins.<slug>`` and calls its top-level :func:`register`
with a ``PluginContext``. The context's :meth:`register_platform`
forwards into ``gateway.platform_registry``.

The inner ``hermes_plugin_xmpp.adapter.register`` was written for an
older / different contract — it ignores its ``ctx`` argument and
returns a dict describing the platform. PluginContext does not look
at return values, so calling it directly silently no-ops. This shim
calls the inner register to harvest the descriptor, translates its
keys onto the ``register_platform()`` keyword signature, and adds
the Hermes-side metadata (``required_env``, allowed-user env names,
cron home env, emoji, install hint) that the inner descriptor
doesn't surface.
"""
from __future__ import annotations

from typing import Any

from hermes_plugin_xmpp.adapter import register as _inner_register

_DESCRIPTOR_TO_KWARG = {
    "name": "name",
    "label": "label",
    "factory": "adapter_factory",
    "is_configured": "check_fn",
    "validate": "validate_config",
    "interactive_setup": "setup_fn",
    "env_enable_hook": "env_enablement_fn",
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

    kwargs.setdefault("required_env", ["XMPP_JID", "XMPP_PASSWORD"])
    kwargs.setdefault("install_hint", "uv pip install --python <hermes-venv>/bin/python slixmpp aiohttp")
    kwargs.setdefault("allowed_users_env", "XMPP_ALLOWED_JIDS")
    kwargs.setdefault("allow_all_env", "XMPP_ALLOW_ALL_USERS")
    kwargs.setdefault("cron_deliver_env_var", "XMPP_HOME_JID")
    kwargs.setdefault("emoji", "💬")

    ctx.register_platform(**kwargs)


__all__ = ["register"]

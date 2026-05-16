"""Out-of-process sender used by Hermes's cron / notification path when the
long-running gateway is not the process delivering the message.

Mirrors the IRC plugin's ``_standalone_send`` pattern: open a short-lived
slixmpp client, wait for session_start, send one stanza, then disconnect.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from .config import XmppConfig, load_config
from .jid_utils import parse_jid

log = logging.getLogger(__name__)

_STANDALONE_RESOURCE_SUFFIX = "-cron"


def _build_client(cfg: XmppConfig):
    """Imported lazily so ``import hermes_plugin_xmpp`` does not pull in
    slixmpp until the gateway actually constructs an adapter."""
    from slixmpp import ClientXMPP

    parsed = parse_jid(cfg.jid)
    resource = (parsed.resource or cfg.resource) + _STANDALONE_RESOURCE_SUFFIX
    bot_jid = f"{parsed.bare}/{resource}"
    client = ClientXMPP(bot_jid, cfg.password)
    client.register_plugin("xep_0030")
    client.register_plugin("xep_0199")
    return client


async def _send_once(cfg: XmppConfig, recipient: str, body: str, *, mtype: str) -> bool:
    client = _build_client(cfg)
    done: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

    def _on_session_start(_event):
        try:
            client.send_presence()
            client.send_message(mto=recipient, mbody=body, mtype=mtype)
        finally:
            # Give the stanza a moment to flush before we disconnect.
            asyncio.get_event_loop().call_later(0.5, lambda: client.disconnect(wait=True))

    def _on_disconnected(_event):
        if not done.done():
            done.set_result(True)

    def _on_failed_auth(_event):
        if not done.done():
            done.set_exception(RuntimeError("XMPP auth failed"))

    client.add_event_handler("session_start", _on_session_start)
    client.add_event_handler("disconnected", _on_disconnected)
    client.add_event_handler("failed_auth", _on_failed_auth)

    host = cfg.server_or_domain()
    client.connect(address=(host, cfg.port) if host else None, use_ssl=False)

    try:
        return await asyncio.wait_for(done, timeout=30)
    except asyncio.TimeoutError:
        log.warning("XMPP standalone send to %s timed out", recipient)
        try:
            client.disconnect(wait=False)
        except Exception:
            pass
        return False


def standalone_send(
    recipient: str,
    body: str,
    *,
    extra: Mapping[str, object] | None = None,
    is_muc: bool = False,
) -> bool:
    """Sync entry point matching the plugin loader's ``standalone_sender``
    contract. Spins up its own event loop because the cron path is
    expected to be synchronous."""
    cfg = load_config(extra)
    parse_jid(recipient)  # validate
    mtype = "groupchat" if is_muc else "chat"
    return asyncio.run(_send_once(cfg, recipient, body, mtype=mtype))

"""Out-of-process XMPP sender.

The gateway's cron / notification path uses this when the long-running
adapter is not the process delivering the message. Signature matches the
``standalone_sender_fn`` contract documented in
``gateway.platform_registry.PlatformEntry``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from .config import XmppConfig, load_config
from .jid_utils import parse_jid

log = logging.getLogger(__name__)

_STANDALONE_RESOURCE_SUFFIX = "-cron"


def _build_client(cfg: XmppConfig):
    """Imported lazily so ``import hermes_plugin_xmpp`` doesn't pull in
    slixmpp until the gateway actually constructs a sender."""
    from slixmpp import ClientXMPP

    parsed = parse_jid(cfg.jid)
    resource = (parsed.resource or cfg.resource) + _STANDALONE_RESOURCE_SUFFIX
    bot_jid = f"{parsed.bare}/{resource}"
    client = ClientXMPP(bot_jid, cfg.password)
    client.register_plugin("xep_0030")
    client.register_plugin("xep_0199")
    return client


async def _send_once(
    cfg: XmppConfig, recipient: str, body: str, *, mtype: str
) -> dict[str, Any]:
    client = _build_client(cfg)
    done: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
    sent_id_holder: dict[str, str] = {}

    def _on_session_start(_event):
        try:
            client.send_presence()
            msg = client.make_message(mto=recipient, mbody=body, mtype=mtype)
            msg.send()
            sent_id_holder["id"] = str(msg.get("id", "")) or ""
        except Exception as exc:
            if not done.done():
                done.set_result({"error": f"send failed: {exc}"})
            return
        finally:
            asyncio.get_event_loop().call_later(0.5, lambda: client.disconnect(wait=True))

    def _on_disconnected(_event):
        if not done.done():
            done.set_result({"success": True, "message_id": sent_id_holder.get("id") or None})

    def _on_failed_auth(_event):
        if not done.done():
            done.set_result({"error": "XMPP auth failed"})

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
        return {"error": "timed out waiting for XMPP send"}


async def standalone_sender_fn(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: Any = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Async entry point matching :class:`PlatformEntry.standalone_sender_fn`.

    XMPP has no native thread/media-document concept here — those kwargs
    are accepted for contract compatibility and ignored. ``pconfig`` is
    the gateway's ``PlatformConfig``; its ``.extra`` (or, in dev, the env)
    must carry credentials.
    """
    del thread_id, media_files, force_document
    extra: Mapping[str, Any] = getattr(pconfig, "extra", None) or {}
    try:
        cfg = load_config(extra)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        parse_jid(chat_id)
    except ValueError as exc:
        return {"error": f"invalid recipient JID: {exc}"}
    mtype = "groupchat" if _looks_like_room(cfg, chat_id) else "chat"
    return await _send_once(cfg, chat_id, message, mtype=mtype)


def _looks_like_room(cfg: XmppConfig, chat_id: str) -> bool:
    target = chat_id.lower()
    for room in cfg.muc_rooms:
        try:
            if parse_jid(room).bare.lower() == target:
                return True
        except ValueError:
            continue
    return False

"""Hermes XMPP gateway adapter.

Wraps a :class:`slixmpp.ClientXMPP` instance and adapts it to the Hermes
:class:`BasePlatformAdapter` contract documented in
``gateway/platforms/base.py`` of the Hermes core, plus the plugin loader
hooks documented in ``gateway/platforms/ADDING_A_PLATFORM.md``.

The closest in-tree analog is ``plugins/platforms/irc/adapter.py``; this
file follows its ``register()``-returns-dict pattern, reconnect-with-
backoff loop, and standalone cron sender.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .config import XmppConfig, is_configured, load_config, validate
from .history import (
    DedupLRU,
    LastSeenStore,
    StanzaKey,
    collect_mam_results,
    iter_carbon_payload,
    replay_should_respond,
)
from .jid_utils import (
    PLATFORM,
    bare_jid,
    build_session_key,
    chat_id_for_dm,
    chat_id_for_muc,
    is_addressed_to_nick,
    parse_jid,
    strip_nick_prefix,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BasePlatformAdapter import shim.
#
# At runtime the gateway exposes ``gateway.platforms.base.BasePlatformAdapter``
# along with a ``SendResult`` dataclass. When the plugin is imported outside
# the gateway (e.g. by unit tests) we substitute a minimal stub so the module
# loads cleanly.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised only when installed alongside Hermes
    from gateway.platforms.base import BasePlatformAdapter, SendResult  # type: ignore
except Exception:  # pragma: no cover
    @dataclass
    class SendResult:  # type: ignore[no-redef]
        ok: bool = True
        message_ids: list[str] = field(default_factory=list)
        error: str | None = None

    class BasePlatformAdapter:  # type: ignore[no-redef]
        """Minimal stand-in used only when the real gateway is not installed."""

        def __init__(self) -> None:
            self._message_handler: Callable[[Any], Awaitable[None]] | None = None
            self._busy_handler: Callable[[Any], Awaitable[None]] | None = None
            self._session_store: Any = None

        def set_message_handler(self, handler: Callable[[Any], Awaitable[None]]) -> None:
            self._message_handler = handler

        def set_busy_session_handler(self, handler: Callable[[Any], Awaitable[None]]) -> None:
            self._busy_handler = handler

        def set_session_store(self, store: Any) -> None:
            self._session_store = store


# ---------------------------------------------------------------------------
# MessageEvent — the gateway's real event class lives in the core, but we
# only need a small dict-shaped object that the gateway accepts. The fields
# we set are the union of what BasePlatformAdapter callers expect.
# ---------------------------------------------------------------------------
@dataclass
class XmppMessageEvent:
    platform: str
    chat_type: str
    chat_id: str
    session_key: str
    sender_jid: str
    sender_display: str
    text: str
    raw_id: str
    is_muc: bool
    addressed_to_bot: bool
    replay: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


MessageHandler = Callable[[XmppMessageEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Markdown stripper. XMPP allows XHTML-IM but client support is uneven, and
# the Telegram/IRC adapters both default to plain text — match that.
# ---------------------------------------------------------------------------
_MD_BOLD_OR_ITALIC = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_FENCED = re.compile(r"```[a-zA-Z0-9_-]*\n([\s\S]*?)\n```", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    text = _MD_FENCED.sub(lambda m: m.group(1), text)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_INLINE_CODE.sub(lambda m: m.group(1), text)
    text = _MD_BOLD_OR_ITALIC.sub(lambda m: m.group(2), text)
    text = _MD_HEADER.sub("", text)
    return text


# Conservative chunk size. XMPP itself has no body limit, but many servers
# cap stanza size around 10 KiB; 4 KiB leaves room for envelope overhead.
_CHUNK_BYTES = 4096


def _chunk(text: str, limit: int = _CHUNK_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        line_size = len(line.encode("utf-8"))
        if line_size > limit:
            if buf:
                parts.append("".join(buf))
                buf, size = [], 0
            # Split very long line on UTF-8 boundaries.
            data = line.encode("utf-8")
            for i in range(0, len(data), limit):
                parts.append(data[i : i + limit].decode("utf-8", errors="ignore"))
            continue
        if size + line_size > limit and buf:
            parts.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += line_size
    if buf:
        parts.append("".join(buf))
    return parts


# Backoff sequence matches the IRC adapter: 2s, 4s, 8s, 16s, 32s capped.
_BACKOFF_SCHEDULE = (2, 4, 8, 16, 32)


class XmppAdapter(BasePlatformAdapter):
    """Hermes gateway adapter for XMPP."""

    def __init__(self, cfg: XmppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self._client: Any | None = None
        self._connected_event: asyncio.Event = asyncio.Event()
        self._stopping: bool = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._dedup = DedupLRU()
        self._last_seen = LastSeenStore(cfg.bare_jid)
        self._typing_chats: set[str] = set()

    # -- BasePlatformAdapter API ---------------------------------------------

    async def connect(self) -> bool:
        try:
            from slixmpp import ClientXMPP
        except ImportError as exc:
            log.error("XMPP plugin: slixmpp is not installed (%s)", exc)
            return False

        parsed = parse_jid(self.cfg.jid)
        client_jid = f"{parsed.bare}/{parsed.resource or self.cfg.resource}"
        client = ClientXMPP(client_jid, self.cfg.password)

        for plugin in (
            "xep_0030",  # service discovery
            "xep_0045",  # MUC
            "xep_0048",  # bookmarks
            "xep_0066",  # OOB data (image previews)
            "xep_0071",  # XHTML-IM (optional rendering)
            "xep_0085",  # chat states (typing)
            "xep_0199",  # ping
            "xep_0280",  # message carbons
            "xep_0313",  # MAM
            "xep_0359",  # stanza IDs (for dedup)
            "xep_0363",  # HTTP upload
        ):
            with suppress(Exception):
                client.register_plugin(plugin)

        client.add_event_handler("session_start", self._on_session_start)
        client.add_event_handler("message", self._on_message)
        client.add_event_handler("groupchat_message", self._on_muc_message)
        client.add_event_handler("failed_auth", self._on_failed_auth)
        client.add_event_handler("disconnected", self._on_disconnected)

        self._client = client
        self._stopping = False

        host = self.cfg.server_or_domain()
        try:
            client.connect(address=(host, self.cfg.port) if host else None, use_ssl=False)
        except Exception as exc:
            log.error("XMPP plugin: connect() failed: %s", exc)
            return False

        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            log.error("XMPP plugin: timed out waiting for session_start")
            return False

        return True

    async def disconnect(self) -> None:
        self._stopping = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
        client = self._client
        if client is None:
            return
        try:
            for room in self.cfg.muc_rooms:
                with suppress(Exception):
                    client["xep_0045"].leave_muc(parse_jid(room).bare, self.cfg.muc_nickname)
            client.send_presence(ptype="unavailable")
        except Exception as exc:
            log.debug("XMPP plugin: teardown presence failed: %s", exc)
        with suppress(Exception):
            client.disconnect(wait=True)
        self._client = None
        self._connected_event.clear()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        client = self._client
        if client is None:
            return SendResult(ok=False, error="XMPP client not connected")

        is_muc = bool(metadata and metadata.get("is_muc")) or self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"
        body = _strip_markdown(content or "")
        if not body.strip():
            return SendResult(ok=True, message_ids=[])

        message_ids: list[str] = []
        for chunk in _chunk(body):
            try:
                msg = client.make_message(mto=chat_id, mbody=chunk, mtype=mtype)
                if reply_to:
                    with suppress(Exception):
                        msg["reply"]["id"] = reply_to
                        msg["reply"]["to"] = chat_id
                msg.send()
                msg_id = str(msg.get("id", "")) or ""
                if msg_id:
                    message_ids.append(msg_id)
            except Exception as exc:
                log.warning("XMPP plugin: send() failed: %s", exc)
                return SendResult(ok=False, message_ids=message_ids, error=str(exc))
            await asyncio.sleep(0)  # let the writer flush between chunks

        return SendResult(ok=True, message_ids=message_ids)

    # -- Optional capabilities -----------------------------------------------

    async def send_typing(self, chat_id: str) -> None:
        client = self._client
        if client is None:
            return
        is_muc = self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"
        with suppress(Exception):
            client.send_message(mto=chat_id, mtype=mtype, mbody=None, mfrom=None,
                                mnick=None, mhtml=None, msubject=None)
        # XEP-0085 chat state
        with suppress(Exception):
            msg = client.make_message(mto=chat_id, mtype=mtype)
            msg["chat_state"] = "composing"
            msg.send()
        self._typing_chats.add(chat_id)

    async def stop_typing(self, chat_id: str) -> None:
        client = self._client
        if client is None:
            return
        if chat_id not in self._typing_chats:
            return
        is_muc = self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"
        with suppress(Exception):
            msg = client.make_message(mto=chat_id, mtype=mtype)
            msg["chat_state"] = "paused"
            msg.send()
        self._typing_chats.discard(chat_id)

    async def send_image_file(
        self,
        chat_id: str,
        path: str,
        caption: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        client = self._client
        if client is None:
            return SendResult(ok=False, error="XMPP client not connected")
        if not os.path.isfile(path):
            return SendResult(ok=False, error=f"image file not found: {path}")

        url: str | None = None
        try:
            upload = client["xep_0363"]
            # Let slixmpp's XEP-0363 plugin discover the upload service via
            # disco — ejabberd's ``mod_http_upload`` lives on its own
            # component subdomain (e.g. ``upload.chat.rzem.ai``), not the
            # user's own domain, so hard-coding ``domain=self.cfg.domain``
            # would miss it.
            url = await upload.upload_file(path)
        except Exception as exc:
            log.warning("XMPP plugin: HTTP upload failed (%s); sending caption only", exc)

        is_muc = bool(metadata and metadata.get("is_muc")) or self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"

        body_parts = []
        if caption:
            body_parts.append(caption)
        if url:
            body_parts.append(url)
        body = "\n".join(body_parts) if body_parts else (url or "")
        if not body:
            return SendResult(ok=False, error="nothing to send")

        try:
            msg = client.make_message(mto=chat_id, mbody=body, mtype=mtype)
            if url:
                with suppress(Exception):
                    msg["oob"]["url"] = url
            msg.send()
            return SendResult(ok=True, message_ids=[str(msg.get("id", ""))])
        except Exception as exc:
            return SendResult(ok=False, error=str(exc))

    # -- slixmpp event handlers ----------------------------------------------

    async def _on_session_start(self, _event: Any) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.send_presence()
            await client.get_roster()
        except Exception as exc:
            log.debug("XMPP plugin: presence/roster setup: %s", exc)

        # Enable Message Carbons.
        with suppress(Exception):
            await client["xep_0280"].enable()

        # MAM catch-up (DM scope).
        await self._mam_catchup(scope=None, with_jid=None)

        # Join configured MUC rooms.
        for room in self.cfg.muc_rooms:
            room_bare = parse_jid(room).bare
            try:
                await client["xep_0045"].join_muc_wait(
                    room_bare, self.cfg.muc_nickname, maxhistory="0"
                )
                log.info("XMPP plugin: joined MUC %s as %s", room_bare, self.cfg.muc_nickname)
            except Exception as exc:
                log.warning("XMPP plugin: could not join %s: %s", room_bare, exc)
                continue
            # MAM catch-up per room.
            await self._mam_catchup(scope=f"muc:{room_bare.lower()}", with_jid=room_bare)

        self._connected_event.set()

    async def _on_failed_auth(self, _event: Any) -> None:
        log.error("XMPP plugin: authentication failed for %s", self.cfg.bare_jid)
        self._stopping = True
        self._connected_event.set()  # unblock connect() so it returns False

    async def _on_disconnected(self, _event: Any) -> None:
        self._connected_event.clear()
        if self._stopping:
            return
        log.warning("XMPP plugin: disconnected; scheduling reconnect")
        self._reconnect_task = asyncio.create_task(self._reconnect_with_backoff())

    async def _reconnect_with_backoff(self) -> None:
        attempt = 0
        while not self._stopping:
            delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
            await asyncio.sleep(delay)
            attempt += 1
            log.info("XMPP plugin: reconnect attempt %d", attempt)
            try:
                if await self.connect():
                    log.info("XMPP plugin: reconnected after %d attempts", attempt)
                    return
            except Exception as exc:
                log.warning("XMPP plugin: reconnect failed: %s", exc)

    async def _on_message(self, stanza: Any) -> None:
        """Incoming 1:1 message, possibly wrapped in a Carbon."""
        carbon_dir, inner = iter_carbon_payload(stanza)
        if carbon_dir == "sent":
            # Our own outbound message echoed from another client; record
            # for dedup so MAM doesn't replay it as user input.
            self._dedup.mark(StanzaKey.from_stanza(inner))
            return
        target = inner if carbon_dir == "received" else stanza
        try:
            await self._dispatch_dm(target, replay=False)
        except Exception:
            log.exception("XMPP plugin: error dispatching DM")

    async def _on_muc_message(self, stanza: Any) -> None:
        try:
            await self._dispatch_muc(stanza, replay=False)
        except Exception:
            log.exception("XMPP plugin: error dispatching MUC message")

    # -- Dispatch helpers ----------------------------------------------------

    async def _dispatch_dm(self, stanza: Any, *, replay: bool) -> None:
        body = self._extract_body(stanza)
        if body is None:
            return
        from_jid = str(stanza.get("from", "")) if hasattr(stanza, "get") else ""
        if not from_jid:
            return
        try:
            sender_bare = bare_jid(from_jid)
        except ValueError:
            return
        if sender_bare.lower() == self.cfg.bare_jid.lower():
            return  # ignore stanzas from ourselves
        if not self._is_allowed(sender_bare):
            log.info("XMPP plugin: denied DM from %s (not in XMPP_ALLOWED_JIDS)", sender_bare)
            return

        key = StanzaKey.from_stanza(stanza)
        if not self._dedup.mark(key):
            return

        chat_type, chat_id = chat_id_for_dm(from_jid)
        event = XmppMessageEvent(
            platform=PLATFORM,
            chat_type=chat_type,
            chat_id=chat_id,
            session_key=build_session_key(chat_type, chat_id),
            sender_jid=sender_bare,
            sender_display=sender_bare,
            text=body,
            raw_id=str(stanza.get("id", "")) if hasattr(stanza, "get") else "",
            is_muc=False,
            addressed_to_bot=True,
            replay=replay,
        )
        await self._update_cursor(scope=None, stanza=stanza)
        await self._deliver(event, stanza)

    async def _dispatch_muc(self, stanza: Any, *, replay: bool) -> None:
        body = self._extract_body(stanza)
        if body is None:
            return
        from_jid = str(stanza.get("from", "")) if hasattr(stanza, "get") else ""
        if not from_jid:
            return
        room_jid, _, nick = from_jid.partition("/")
        if not room_jid:
            return
        if nick and nick == self.cfg.muc_nickname:
            return  # our own MUC echo

        addressed = is_addressed_to_nick(body, self.cfg.muc_nickname)
        if not addressed:
            return

        # In MUC, the real sender is opaque (the room rewrites from=); we
        # use the room JID for authorization, since MUC membership is the
        # bouncer. allow_all_users bypasses if set.
        if not self.cfg.allow_all_users and self.cfg.allowed_jids:
            # Optional stricter check: allow MUC traffic unconditionally, since
            # joining the room is itself an access check. Log for visibility.
            log.debug("XMPP plugin: MUC message in %s (per-room allow lists not enforced)", room_jid)

        key = StanzaKey.from_stanza(stanza)
        if not self._dedup.mark(key):
            return

        text = strip_nick_prefix(body, self.cfg.muc_nickname)
        chat_type, chat_id = chat_id_for_muc(room_jid)
        event = XmppMessageEvent(
            platform=PLATFORM,
            chat_type=chat_type,
            chat_id=chat_id,
            session_key=build_session_key(chat_type, chat_id),
            sender_jid=from_jid,
            sender_display=nick or from_jid,
            text=text,
            raw_id=str(stanza.get("id", "")) if hasattr(stanza, "get") else "",
            is_muc=True,
            addressed_to_bot=True,
            replay=replay,
        )
        await self._update_cursor(scope=f"muc:{room_jid.lower()}", stanza=stanza)
        await self._deliver(event, stanza)

    async def _deliver(self, event: XmppMessageEvent, stanza: Any) -> None:
        handler = getattr(self, "_message_handler", None)
        if handler is None:
            log.debug("XMPP plugin: no message handler registered; dropping %s", event.chat_id)
            return

        if event.replay:
            ts = _stanza_timestamp(stanza)
            if not replay_should_respond(ts, grace_seconds=self.cfg.mam_replay_grace_seconds):
                # Ingest silently: still notify the gateway but mark it.
                event.extra["silent"] = True

        try:
            await handler(event)
        except Exception:
            log.exception("XMPP plugin: gateway handler raised for %s", event.chat_id)

    # -- MAM catch-up --------------------------------------------------------

    async def _mam_catchup(self, *, scope: str | None, with_jid: str | None) -> None:
        client = self._client
        if client is None:
            return
        cursor = self._last_seen.get(scope=scope)
        start = cursor.get("ts_iso") or cursor.get("ts")
        try:
            mam = client["xep_0313"]
        except Exception:
            return

        try:
            results = await mam.retrieve(
                with_jid=with_jid,
                start=start,
                iterator=False,
                rsm={"max": self.cfg.mam_catchup_limit},
            )
        except Exception as exc:
            log.debug("XMPP plugin: MAM catch-up (%s) failed: %s", scope or "dm", exc)
            return

        # slixmpp's API returns either a list of forwarded stanzas or a
        # results envelope; ``collect_mam_results`` copes with both.
        try:
            inner_list = list(getattr(results, "results", None) or results)
        except TypeError:
            inner_list = []
        forwarded = collect_mam_results(inner_list)
        forwarded = forwarded[: self.cfg.mam_catchup_limit]
        if not forwarded:
            return

        log.info("XMPP plugin: replaying %d MAM stanza(s) for scope=%s",
                 len(forwarded), scope or "dm")
        for stanza in forwarded:
            mtype = (stanza.get("type") if hasattr(stanza, "get") else None) or "chat"
            try:
                if mtype == "groupchat":
                    await self._dispatch_muc(stanza, replay=True)
                else:
                    await self._dispatch_dm(stanza, replay=True)
            except Exception:
                log.exception("XMPP plugin: MAM replay item failed")

    async def _update_cursor(self, *, scope: str | None, stanza: Any) -> None:
        sid = None
        try:
            sid = stanza["stanza_id"]["id"]
        except Exception:
            pass
        ts = _stanza_timestamp(stanza)
        if sid or ts is not None:
            self._last_seen.update(scope, stanza_id=sid, ts=ts)

    # -- Helpers -------------------------------------------------------------

    def _is_allowed(self, sender_bare: str) -> bool:
        if self.cfg.allow_all_users:
            return True
        if not self.cfg.allowed_jids:
            return False
        return sender_bare.lower() in self.cfg.allowed_jids

    def _looks_like_muc(self, chat_id: str) -> bool:
        target = chat_id.lower()
        for room in self.cfg.muc_rooms:
            with suppress(ValueError):
                if parse_jid(room).bare.lower() == target:
                    return True
        return False

    @staticmethod
    def _extract_body(stanza: Any) -> str | None:
        try:
            body = stanza["body"]
        except Exception:
            return None
        if not body:
            return None
        body = str(body).strip()
        if not body:
            return None
        return body


def _stanza_timestamp(stanza: Any) -> float | None:
    """Pull a UTC timestamp from a stanza if it carries an XEP-0203 delay."""
    try:
        delay = stanza["delay"]
        stamp = delay["stamp"] if delay is not None else None
    except Exception:
        stamp = None
    if not stamp:
        return None
    from datetime import datetime

    if isinstance(stamp, datetime):
        return stamp.timestamp()
    try:
        s = str(stamp).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Plugin loader entry point.
#
# Hermes's plugin loader calls ``register(ctx)`` and expects back a dict
# describing the platform, factory, and optional hooks. The IRC plugin in
# ``plugins/platforms/irc/adapter.py`` is the canonical reference for the
# shape used here.
# ---------------------------------------------------------------------------
PLATFORM_HINT = (
    "You are talking through XMPP. Use plain text — XHTML-IM and markdown "
    "tables are only rendered by some clients. Keep messages concise and "
    "avoid embedded HTML."
)


def _factory(cfg_extra: Mapping[str, Any] | None) -> XmppAdapter:
    return XmppAdapter(load_config(cfg_extra))


def _env_enable_hook(env: dict[str, str], yaml_extra: Mapping[str, Any] | None) -> None:
    """Seed environment variables from the gateway-supplied YAML so the
    adapter sees a consistent view regardless of how the user configured
    things. Called by the gateway before factory()."""
    yaml = yaml_extra or {}
    mapping = {
        "XMPP_JID": "jid",
        "XMPP_PASSWORD": "password",
        "XMPP_SERVER": "server",
        "XMPP_PORT": "port",
        "XMPP_USE_TLS": "use_tls",
        "XMPP_RESOURCE": "resource",
        "XMPP_MUC_ROOMS": "muc_rooms",
        "XMPP_MUC_NICKNAME": "muc_nickname",
        "XMPP_ALLOWED_JIDS": "allowed_jids",
        "XMPP_ALLOW_ALL_USERS": "allow_all_users",
        "XMPP_HOME_JID": "home_jid",
        "XMPP_MAM_REPLAY_GRACE_SECONDS": "mam_replay_grace_seconds",
        "XMPP_MAM_CATCHUP_LIMIT": "mam_catchup_limit",
    }
    for env_key, yaml_key in mapping.items():
        if env.get(env_key):
            continue
        value = yaml.get(yaml_key)
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple, set)):
            env[env_key] = ",".join(str(x) for x in value)
        else:
            env[env_key] = str(value)


def _interactive_setup() -> dict[str, str]:
    """CLI wizard invoked by ``hermes gateway setup`` when the user picks
    XMPP. Returns the env-var assignments to persist to ``~/.hermes/.env``."""
    import getpass

    def ask(prompt: str, *, default: str = "", secret: bool = False) -> str:
        suffix = f" [{default}]" if default and not secret else ""
        if secret:
            return getpass.getpass(f"{prompt}: ").strip() or default
        return input(f"{prompt}{suffix}: ").strip() or default

    jid = ask("Bot JID (e.g. hermes@chat.rzem.ai)")
    password = ask("Bot password", secret=True)
    server = ask("Server hostname (blank = derive from JID)")
    port = ask("Server port", default="5222")
    resource = ask("Resource", default="hermes")
    muc = ask("MUC rooms to join (comma-separated, blank = none)")
    muc_nick = ask("MUC nickname", default="hermes-bot")
    allowed = ask("Allowed bare JIDs (comma-separated)")
    home = ask("Home JID for cron / notifications (blank = first allowed)")

    out: dict[str, str] = {
        "XMPP_JID": jid,
        "XMPP_PASSWORD": password,
    }
    if server:
        out["XMPP_SERVER"] = server
    if port and port != "5222":
        out["XMPP_PORT"] = port
    if resource and resource != "hermes":
        out["XMPP_RESOURCE"] = resource
    if muc:
        out["XMPP_MUC_ROOMS"] = muc
        out["XMPP_MUC_NICKNAME"] = muc_nick
    if allowed:
        out["XMPP_ALLOWED_JIDS"] = allowed
    if home:
        out["XMPP_HOME_JID"] = home
    return out


def _standalone_send(recipient: str, body: str, *, extra: Mapping[str, Any] | None = None,
                     is_muc: bool = False) -> bool:
    from ._standalone import standalone_send

    return standalone_send(recipient, body, extra=extra, is_muc=is_muc)


def register(ctx: Any = None) -> dict[str, Any]:
    """Hermes plugin entry point.

    The ``ctx`` argument is the plugin context object the gateway passes
    in; we accept it for forward compatibility but do not currently need
    anything from it.
    """
    del ctx  # currently unused
    return {
        "name": PLATFORM,
        "label": "XMPP",
        "kind": "platform",
        "factory": _factory,
        "is_configured": is_configured,
        "validate": validate,
        "interactive_setup": _interactive_setup,
        "env_enable_hook": _env_enable_hook,
        "standalone_sender": _standalone_send,
        "platform_hint": PLATFORM_HINT,
    }

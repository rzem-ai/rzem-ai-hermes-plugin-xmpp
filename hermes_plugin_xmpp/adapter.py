"""Hermes XMPP gateway adapter.

Wraps a :class:`slixmpp.ClientXMPP` instance and adapts it to Hermes's
:class:`gateway.platforms.base.BasePlatformAdapter` contract. Closest
in-tree reference is ``plugins/platforms/irc/adapter.py``.

Inbound stanzas are normalised into a real :class:`MessageEvent` with a
:class:`SessionSource` and handed to ``self.handle_message`` — the same
entry point every built-in adapter uses. The gateway is the single source
of truth for session keys, routing, and (where configured) cross-platform
mirroring; the adapter's job is to deliver clean events and accept clean
sends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from ._compat import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    Platform,
    SendResult,
    SessionSource,
)
from .config import XmppConfig, is_configured, load_config, validate
from .history import (
    DedupLRU,
    LastSeenStore,
    StanzaKey,
    collect_mam_results,
    iter_carbon_payload,
    replay_should_respond,
    stanza_timestamp,
)
from .jid_utils import (
    PLATFORM,
    bare_jid,
    is_addressed_to_nick,
    parse_jid,
    strip_nick_prefix,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Markdown stripper. XMPP allows XHTML-IM but client support is uneven, and
# IRC defaults to plain text — match that for symmetry.
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


def _split_utf8_chunk(data: bytes, limit: int) -> list[bytes]:
    """Slice ``data`` into ``<= limit`` byte chunks on UTF-8 boundaries.

    The naive ``data[i:i+limit]`` slice can land in the middle of a
    multi-byte code point. We back up to the last code-point start byte
    so every chunk decodes cleanly.
    """
    out: list[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        end = min(i + limit, n)
        if end < n:
            # Continuation bytes are 0b10xxxxxx (0x80-0xBF). Back up until
            # we land on a start byte.
            while end > i and (data[end] & 0xC0) == 0x80:
                end -= 1
            if end == i:
                # Single code point larger than limit — shouldn't happen
                # for real UTF-8, but emit the raw slice to make progress.
                end = min(i + limit, n)
        out.append(data[i:end])
        i = end
    return out


def _chunk(text: str, limit: int = _CHUNK_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        line_bytes = line.encode("utf-8")
        if len(line_bytes) > limit:
            if buf:
                parts.append("".join(buf))
                buf, size = [], 0
            for piece in _split_utf8_chunk(line_bytes, limit):
                parts.append(piece.decode("utf-8"))
            continue
        if size + len(line_bytes) > limit and buf:
            parts.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line_bytes)
    if buf:
        parts.append("".join(buf))
    return parts


# Backoff schedule for reconnect — matches the IRC adapter.
_BACKOFF_SCHEDULE = (2, 4, 8, 16, 32)


class XmppAdapter(BasePlatformAdapter):
    """Hermes gateway adapter for XMPP."""

    def __init__(self, config: Any, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("xmpp"))
        extra = getattr(config, "extra", None) or {}
        # Tests construct the adapter directly with an XmppConfig; the live
        # gateway passes a PlatformConfig whose ``.extra`` carries the YAML.
        if isinstance(config, XmppConfig):
            self.cfg = config
        else:
            self.cfg = load_config(extra)
        self._client: Any | None = None
        self._connected_event: asyncio.Event = asyncio.Event()
        self._stopping: bool = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._dedup = DedupLRU()
        self._last_seen = LastSeenStore(self.cfg.bare_jid)
        self._typing_chats: set[str] = set()

    @property
    def name(self) -> str:
        return "XMPP"

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
            client.connect(host=host or None, port=self.cfg.port)
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
            return SendResult(success=False, error="XMPP client not connected", retryable=True)

        is_muc = bool(metadata and metadata.get("is_muc")) or self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"
        body = _strip_markdown(content or "")
        if not body.strip():
            return SendResult(success=True)

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
                return SendResult(
                    success=False,
                    message_id=message_ids[-1] if message_ids else None,
                    continuation_message_ids=tuple(message_ids[:-1]),
                    error=str(exc),
                    retryable=True,
                )
            await asyncio.sleep(0)  # let the writer flush between chunks

        last = message_ids[-1] if message_ids else None
        prefix = tuple(message_ids[:-1])
        return SendResult(success=True, message_id=last, continuation_message_ids=prefix)

    # -- Optional capabilities -----------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return a minimal chat descriptor for ``chat_id``.

        XMPP has no central directory; classify by JID structure.
        """
        is_muc = self._looks_like_muc(chat_id)
        try:
            bare = parse_jid(chat_id).bare
        except Exception:
            bare = chat_id
        if is_muc:
            local = bare.split("@", 1)[0] if "@" in bare else bare
            return {"name": local or bare, "type": "group", "id": bare}
        return {"name": bare, "type": "dm", "id": bare}

    async def send_typing(self, chat_id: str) -> None:
        client = self._client
        if client is None:
            return
        is_muc = self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"
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
            return SendResult(success=False, error="XMPP client not connected", retryable=True)
        if not os.path.isfile(path):
            return SendResult(success=False, error=f"image file not found: {path}")

        url: str | None = None
        try:
            # Let slixmpp's XEP-0363 plugin discover the upload service via
            # disco — ejabberd's mod_http_upload runs on its own component
            # subdomain, not the user's own domain.
            url = await client["xep_0363"].upload_file(path)
        except Exception as exc:
            log.warning("XMPP plugin: HTTP upload failed (%s); sending caption only", exc)

        is_muc = bool(metadata and metadata.get("is_muc")) or self._looks_like_muc(chat_id)
        mtype = "groupchat" if is_muc else "chat"

        body_parts: list[str] = []
        if caption:
            body_parts.append(caption)
        if url:
            body_parts.append(url)
        body = "\n".join(body_parts) if body_parts else (url or "")
        if not body:
            return SendResult(success=False, error="nothing to send")

        try:
            msg = client.make_message(mto=chat_id, mbody=body, mtype=mtype)
            if url:
                with suppress(Exception):
                    msg["oob"]["url"] = url
            msg.send()
            return SendResult(success=True, message_id=str(msg.get("id", "")) or None)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

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

        with suppress(Exception):
            await client["xep_0280"].enable()

        # MAM catch-up (DM scope).
        await self._mam_catchup(scope=None, with_jid=None)

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
        from_jid = self._extract_from(stanza)
        if not from_jid:
            return
        try:
            sender_bare = bare_jid(from_jid)
        except ValueError:
            return
        if sender_bare.lower() == self.cfg.bare_jid.lower():
            return  # our own stanza
        if not self._is_allowed(sender_bare):
            log.info("XMPP plugin: denied DM from %s (not in XMPP_ALLOWED_JIDS)", sender_bare)
            return

        if replay and not self._replay_is_fresh(stanza):
            # Stale history — update cursor + dedup, but don't deliver.
            self._dedup.mark(StanzaKey.from_stanza(stanza))
            self._update_cursor(scope=None, stanza=stanza)
            return

        if not self._dedup.mark(StanzaKey.from_stanza(stanza)):
            return

        raw_id = self._extract_id(stanza)
        source = self.build_source(
            chat_id=sender_bare,
            chat_name=sender_bare,
            chat_type="dm",
            user_id=sender_bare,
            user_name=sender_bare,
            message_id=raw_id or None,
        )
        event = MessageEvent(
            text=body,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=stanza,
            message_id=raw_id or None,
            timestamp=self._event_timestamp(stanza),
        )

        self._update_cursor(scope=None, stanza=stanza)
        await self._deliver(event)

    async def _dispatch_muc(self, stanza: Any, *, replay: bool) -> None:
        body = self._extract_body(stanza)
        if body is None:
            return
        from_jid = self._extract_from(stanza)
        if not from_jid:
            return
        room_jid, _, nick = from_jid.partition("/")
        if not room_jid:
            return
        if nick and nick == self.cfg.muc_nickname:
            return  # our own MUC echo

        addressed = is_addressed_to_nick(body, self.cfg.muc_nickname)
        reply_to_bot = self._reply_targets_bot(stanza)
        if not (addressed or reply_to_bot):
            return

        if replay and not self._replay_is_fresh(stanza):
            self._dedup.mark(StanzaKey.from_stanza(stanza))
            self._update_cursor(scope=f"muc:{room_jid.lower()}", stanza=stanza)
            return

        if not self._dedup.mark(StanzaKey.from_stanza(stanza)):
            return

        text = strip_nick_prefix(body, self.cfg.muc_nickname) if addressed else body
        raw_id = self._extract_id(stanza)
        # In a MUC, ``from`` is ``room@host/nick``. The full MUC JID gives
        # us per-participant session isolation (matching how Telegram groups
        # work with group_sessions_per_user).
        source = self.build_source(
            chat_id=room_jid.lower(),
            chat_name=room_jid.split("@", 1)[0] if "@" in room_jid else room_jid,
            chat_type="group",
            user_id=from_jid,
            user_name=nick or from_jid,
            message_id=raw_id or None,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=stanza,
            message_id=raw_id or None,
            timestamp=self._event_timestamp(stanza),
        )

        self._update_cursor(scope=f"muc:{room_jid.lower()}", stanza=stanza)
        await self._deliver(event)

    async def _deliver(self, event: MessageEvent) -> None:
        try:
            await self.handle_message(event)
        except Exception:
            log.exception(
                "XMPP plugin: gateway handler raised for %s",
                event.source.chat_id if event.source else "<unknown>",
            )

    # -- MAM catch-up --------------------------------------------------------

    async def _mam_catchup(self, *, scope: str | None, with_jid: str | None) -> None:
        client = self._client
        if client is None:
            return
        start = self._last_seen.get_start_iso(scope=scope)
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

        # slixmpp returns either a list of forwarded stanzas or a results
        # envelope; collect_mam_results copes with both.
        try:
            inner_list = list(getattr(results, "results", None) or results)
        except TypeError:
            return
        forwarded = collect_mam_results(inner_list)[: self.cfg.mam_catchup_limit]
        if not forwarded:
            return

        log.info(
            "XMPP plugin: replaying %d MAM stanza(s) for scope=%s",
            len(forwarded),
            scope or "dm",
        )
        for stanza in forwarded:
            mtype = (stanza.get("type") if hasattr(stanza, "get") else None) or "chat"
            try:
                if mtype == "groupchat":
                    await self._dispatch_muc(stanza, replay=True)
                else:
                    await self._dispatch_dm(stanza, replay=True)
            except Exception:
                log.exception("XMPP plugin: MAM replay item failed")

    def _update_cursor(self, *, scope: str | None, stanza: Any) -> None:
        sid: str | None = None
        try:
            sid = stanza["stanza_id"]["id"]
        except Exception:
            pass
        ts = stanza_timestamp(stanza)
        # Fresh (non-replay) stanzas have no delay element; stamp them as
        # "now" so the cursor still advances.
        if ts is None:
            ts = datetime.now(tz=timezone.utc).timestamp()
        if sid or ts is not None:
            self._last_seen.update(scope, stanza_id=sid, ts=ts)

    # -- Helpers -------------------------------------------------------------

    def _replay_is_fresh(self, stanza: Any) -> bool:
        return replay_should_respond(
            stanza_timestamp(stanza),
            grace_seconds=self.cfg.mam_replay_grace_seconds,
        )

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

    def _reply_targets_bot(self, stanza: Any) -> bool:
        """XEP-0461: ``<reply to="..."/>`` lets MUC users address the bot
        without typing the nick prefix. Matches Telegram-style reply UX."""
        try:
            reply_to = stanza["reply"]["to"]
        except Exception:
            return False
        if not reply_to:
            return False
        try:
            return bare_jid(str(reply_to)).lower() == self.cfg.bare_jid.lower()
        except ValueError:
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

    @staticmethod
    def _extract_from(stanza: Any) -> str:
        if not hasattr(stanza, "get"):
            return ""
        return str(stanza.get("from", "") or "")

    @staticmethod
    def _extract_id(stanza: Any) -> str:
        if not hasattr(stanza, "get"):
            return ""
        return str(stanza.get("id", "") or "")

    @staticmethod
    def _event_timestamp(stanza: Any) -> datetime:
        ts = stanza_timestamp(stanza)
        if ts is None:
            return datetime.now(tz=timezone.utc)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Platform hint + interactive setup. These are surfaced by the gateway's
# plugin loader via the outer ``__init__.py`` shim.
# ---------------------------------------------------------------------------
PLATFORM_HINT = (
    "You are talking through XMPP. Use plain text — XHTML-IM and markdown "
    "tables are only rendered by some clients. Keep messages concise and "
    "avoid embedded HTML."
)


def adapter_factory(config: Any) -> XmppAdapter:
    """Hermes adapter factory — ``config`` is the gateway's PlatformConfig."""
    return XmppAdapter(config)


def interactive_setup() -> dict[str, str]:
    """CLI wizard invoked by ``hermes gateway setup`` when the user picks
    XMPP. Returns env-var assignments to persist to ``~/.hermes/.env``."""
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


__all__ = [
    "PLATFORM",
    "PLATFORM_HINT",
    "XmppAdapter",
    "adapter_factory",
    "interactive_setup",
    "is_configured",
    "load_config",
    "validate",
]

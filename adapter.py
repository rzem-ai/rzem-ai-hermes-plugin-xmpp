"""Hermes XMPP gateway adapter — single-file bundle.

Contains everything from hermes_plugin_xmpp: jid_utils, config, history,
_compat, _standalone, and the main XmppAdapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ===========================================================================
# jid_utils
# ===========================================================================

try:
    from slixmpp.jid import JID as _SlixJID
except Exception:  # pragma: no cover
    _SlixJID = None  # type: ignore[assignment]


PLATFORM = "xmpp"


@dataclass(frozen=True)
class ParsedJID:
    bare: str
    local: str
    domain: str
    resource: str

    @property
    def full(self) -> str:
        if self.resource:
            return f"{self.bare}/{self.resource}"
        return self.bare


def parse_jid(value: str) -> ParsedJID:
    """Parse a JID into its components.

    Uses slixmpp's JID (which applies stringprep) when available so the
    runtime behaviour matches the wire. The stdlib fallback exists so unit
    tests don't need slixmpp; it case-folds local/domain to keep
    ``bare_jid()`` consistent with the slixmpp path.
    """
    if not value or not isinstance(value, str):
        raise ValueError("JID must be a non-empty string")

    if _SlixJID is not None:
        jid = _SlixJID(value)
        bare = jid.bare
        if "@" not in bare:
            raise ValueError(f"JID missing local part: {value!r}")
        local, domain = bare.split("@", 1)
        return ParsedJID(bare=bare, local=local, domain=domain, resource=jid.resource or "")

    rest, _, resource = value.partition("/")
    if "@" not in rest:
        raise ValueError(f"JID missing local part: {value!r}")
    local, domain = rest.split("@", 1)
    if not local or not domain:
        raise ValueError(f"Malformed JID: {value!r}")
    local = local.lower()
    domain = domain.lower()
    return ParsedJID(bare=f"{local}@{domain}", local=local, domain=domain, resource=resource)


def bare_jid(value: str) -> str:
    return parse_jid(value).bare


def normalize_jid_set(values: Iterable[str]) -> set[str]:
    """Case-folded bare JIDs for membership checks."""
    out: set[str] = set()
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        out.add(parse_jid(v).bare.lower())
    return out


def is_addressed_to_nick(body: str, nick: str) -> bool:
    """MUC addressing rule.

    A message is "addressed to the bot" when its body opens with the bot's
    nick, optionally preceded by ``@`` and followed by ``:``, ``,``, or
    whitespace. ``@nick`` matches Telegram-style mentions so bridged users
    don't get ignored.
    """
    if not body or not nick:
        return False
    stripped = body.lstrip()
    if stripped.startswith("@"):
        stripped = stripped[1:]
    nick_low = nick.lower()
    head = stripped[: len(nick)].lower()
    if head != nick_low:
        return False
    tail = stripped[len(nick) :]
    if not tail:
        return True
    return tail[0] in (":", ",", " ", "\t")


def strip_nick_prefix(body: str, nick: str) -> str:
    """Remove the leading ``[@]nick[:|,| ]`` addressing prefix, if present."""
    if not is_addressed_to_nick(body, nick):
        return body
    stripped = body.lstrip()
    if stripped.startswith("@"):
        stripped = stripped[1:]
    rest = stripped[len(nick) :]
    if rest and rest[0] in (":", ","):
        rest = rest[1:]
    return rest.lstrip()


# ===========================================================================
# config
# ===========================================================================

DEFAULT_PORT = 5222
DEFAULT_RESOURCE = "hermes"
DEFAULT_MUC_NICKNAME = "hermes-bot"
DEFAULT_MAM_REPLAY_GRACE_SECONDS = 300
DEFAULT_MAM_CATCHUP_LIMIT = 200


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("XMPP plugin: bad int %r, using default %d", value, default)
        return default


def _csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


@dataclass
class XmppConfig:
    jid: str
    password: str
    server: str = ""
    port: int = DEFAULT_PORT
    use_tls: bool = True
    resource: str = DEFAULT_RESOURCE
    muc_rooms: list[str] = field(default_factory=list)
    muc_nickname: str = DEFAULT_MUC_NICKNAME
    allowed_jids: set[str] = field(default_factory=set)
    allow_all_users: bool = False
    home_jid: str = ""
    mam_replay_grace_seconds: int = DEFAULT_MAM_REPLAY_GRACE_SECONDS
    mam_catchup_limit: int = DEFAULT_MAM_CATCHUP_LIMIT

    @property
    def bare_jid(self) -> str:
        return parse_jid(self.jid).bare

    @property
    def domain(self) -> str:
        return parse_jid(self.jid).domain

    def server_or_domain(self) -> str:
        return self.server or self.domain


def _coalesce(env_key: str, yaml_key: str, yaml: Mapping[str, Any]) -> Any:
    env_val = os.environ.get(env_key)
    if env_val not in (None, ""):
        return env_val
    if yaml_key in yaml and yaml[yaml_key] not in (None, ""):
        return yaml[yaml_key]
    return None


def load_config(extra: Mapping[str, Any] | None = None) -> XmppConfig:
    """Build an XmppConfig from environment + the gateway-provided extra dict."""
    yaml: Mapping[str, Any] = extra or {}

    jid_raw = _coalesce("XMPP_JID", "jid", yaml)
    password_raw = _coalesce("XMPP_PASSWORD", "password", yaml)
    if not jid_raw:
        raise ValueError("XMPP plugin: XMPP_JID is required")
    if not password_raw:
        raise ValueError("XMPP plugin: XMPP_PASSWORD is required")

    parsed = parse_jid(str(jid_raw))

    allowed = normalize_jid_set(
        _csv(_coalesce("XMPP_ALLOWED_JIDS", "allowed_jids", yaml))
    )
    home_raw = _coalesce("XMPP_HOME_JID", "home_jid", yaml)
    if home_raw:
        home_jid = parse_jid(str(home_raw)).bare
    elif allowed:
        home_jid = next(iter(sorted(allowed)))
    else:
        home_jid = ""

    return XmppConfig(
        jid=parsed.full,
        password=str(password_raw),
        server=str(_coalesce("XMPP_SERVER", "server", yaml) or ""),
        port=_as_int(_coalesce("XMPP_PORT", "port", yaml), DEFAULT_PORT),
        use_tls=_as_bool(_coalesce("XMPP_USE_TLS", "use_tls", yaml), True),
        resource=str(_coalesce("XMPP_RESOURCE", "resource", yaml) or DEFAULT_RESOURCE),
        muc_rooms=_csv(_coalesce("XMPP_MUC_ROOMS", "muc_rooms", yaml)),
        muc_nickname=str(
            _coalesce("XMPP_MUC_NICKNAME", "muc_nickname", yaml) or DEFAULT_MUC_NICKNAME
        ),
        allowed_jids=allowed,
        allow_all_users=_as_bool(
            _coalesce("XMPP_ALLOW_ALL_USERS", "allow_all_users", yaml), False
        ),
        home_jid=home_jid,
        mam_replay_grace_seconds=_as_int(
            _coalesce("XMPP_MAM_REPLAY_GRACE_SECONDS", "mam_replay_grace_seconds", yaml),
            DEFAULT_MAM_REPLAY_GRACE_SECONDS,
        ),
        mam_catchup_limit=_as_int(
            _coalesce("XMPP_MAM_CATCHUP_LIMIT", "mam_catchup_limit", yaml),
            DEFAULT_MAM_CATCHUP_LIMIT,
        ),
    )


def is_configured(extra: Mapping[str, Any] | None = None) -> bool:
    """Cheap check for the gateway's ``is_configured`` hook."""
    yaml = extra or {}
    jid = os.environ.get("XMPP_JID") or yaml.get("jid")
    password = os.environ.get("XMPP_PASSWORD") or yaml.get("password")
    return bool(jid and password)


def validate(extra: Mapping[str, Any] | None = None) -> list[str]:
    """Return a list of human-readable validation errors (empty == OK)."""
    errors: list[str] = []
    try:
        cfg = load_config(extra)
    except ValueError as exc:
        return [str(exc)]
    try:
        parse_jid(cfg.jid)
    except ValueError as exc:
        errors.append(f"Invalid XMPP_JID: {exc}")
    for room in cfg.muc_rooms:
        try:
            parse_jid(room)
        except ValueError as exc:
            errors.append(f"Invalid MUC room {room!r}: {exc}")
    if cfg.allow_all_users:
        log.warning(
            "XMPP plugin: XMPP_ALLOW_ALL_USERS=true — anyone who can reach %s "
            "will be able to talk to the agent. Use only for development.",
            cfg.bare_jid,
        )
    return errors


# ===========================================================================
# history
# ===========================================================================

DEFAULT_STATE_PATH = Path.home() / ".hermes" / "state" / "xmpp_last_seen.json"
DEFAULT_LRU_SIZE = 5000


@dataclass(frozen=True)
class StanzaKey:
    """Stable identifier for dedup. Prefers XEP-0359 stanza-id, falls back
    to origin-id, falls back to (from, id, timestamp)."""

    value: str

    @classmethod
    def from_stanza(cls, stanza: Any) -> StanzaKey:
        sid = _safe_xep_0359_id(stanza)
        if sid:
            return cls(value=f"sid:{sid}")
        oid = _safe_origin_id(stanza)
        if oid:
            return cls(value=f"oid:{oid}")
        msg_from = str(stanza.get("from", "")) if hasattr(stanza, "get") else ""
        msg_id = str(stanza.get("id", "")) if hasattr(stanza, "get") else ""
        ts = stanza_timestamp(stanza) or time.time()
        return cls(value=f"raw:{msg_from}|{msg_id}|{ts}")


def _safe_xep_0359_id(stanza: Any) -> str | None:
    try:
        sid = stanza["stanza_id"]["id"]
    except Exception:
        return None
    if isinstance(sid, str) and sid:
        return sid
    return None


def _safe_origin_id(stanza: Any) -> str | None:
    try:
        oid = stanza["origin_id"]["id"]
    except Exception:
        return None
    if isinstance(oid, str) and oid:
        return oid
    return None


def stanza_timestamp(stanza: Any) -> float | None:
    """Return the XEP-0203 ``delay`` stamp as a Unix timestamp, or None."""
    try:
        delay = stanza["delay"]
        stamp = delay["stamp"] if delay is not None else None
    except Exception:
        return None
    if not stamp:
        return None
    if isinstance(stamp, datetime):
        return stamp.timestamp()
    try:
        s = str(stamp).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


class DedupLRU:
    """Bounded LRU for stanza dedup. Returns True if the key is new."""

    def __init__(self, max_size: int = DEFAULT_LRU_SIZE) -> None:
        self._max = max_size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def mark(self, key: StanzaKey) -> bool:
        k = key.value
        if k in self._seen:
            self._seen.move_to_end(k)
            return False
        self._seen[k] = None
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._seen)


class LastSeenStore:
    """Persists the most recent stanza we have processed, keyed by
    ``bot_jid`` and ``muc:<room_jid>``."""

    def __init__(self, bot_jid: str, path: Path | None = None) -> None:
        self.bot_jid = bot_jid.lower()
        self.path = path or DEFAULT_STATE_PATH
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._data = {}
            return
        except OSError as exc:
            log.warning("xmpp_last_seen: could not read %s: %s", self.path, exc)
            self._data = {}
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("xmpp_last_seen: corrupt JSON at %s (%s); ignoring", self.path, exc)
            self._data = {}
            return
        if not isinstance(parsed, dict):
            self._data = {}
            return
        self._data = parsed

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("xmpp_last_seen: could not write %s: %s", self.path, exc)

    def _slot(self, scope: str | None) -> str:
        scope = (scope or "dm").lower()
        return f"{self.bot_jid}::{scope}"

    def get(self, scope: str | None = None) -> dict[str, Any]:
        return dict(self._data.get(self._slot(scope), {}))

    def get_start_iso(self, scope: str | None = None) -> str | None:
        """Return the resume cursor as an ISO-8601 string (or None)."""
        entry = self._data.get(self._slot(scope)) or {}
        iso = entry.get("ts_iso")
        return iso if isinstance(iso, str) and iso else None

    def update(self, scope: str | None, *, stanza_id: str | None, ts: float | None) -> None:
        slot = self._slot(scope)
        entry = dict(self._data.get(slot, {}))
        if stanza_id:
            entry["stanza_id"] = stanza_id
        if ts is not None:
            entry["ts_iso"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        if entry:
            self._data[slot] = entry
            self._save()


def replay_should_respond(
    stanza_ts: float | None, *, grace_seconds: int, now: float | None = None
) -> bool:
    """Return True only when a MAM-replayed stanza is fresh enough to act on."""
    if stanza_ts is None:
        return False
    return (now or time.time()) - stanza_ts <= grace_seconds


def iter_carbon_payload(stanza: Any) -> tuple[str | None, Any]:
    """If ``stanza`` is a Message Carbon, return ``(direction, inner_stanza)``
    where ``direction`` is ``"received"`` or ``"sent"``. Otherwise return
    ``(None, stanza)``."""
    for kind in ("carbon_received", "carbon_sent"):
        try:
            present = kind in stanza
        except Exception:
            present = False
        if not present:
            continue
        try:
            inner = stanza[kind]["forwarded"]["stanza"]
        except Exception:
            continue
        if inner is not None:
            return (kind.split("_", 1)[1], inner)
    return (None, stanza)


def collect_mam_results(results: Iterable[Any]) -> list[Any]:
    """Pull forwarded stanzas out of a MAM iterator."""
    out: list[Any] = []
    for result in results:
        try:
            inner = result["mam_result"]["forwarded"]["stanza"]
        except Exception:
            inner = None
        if inner is not None:
            out.append(inner)
    return out


# ===========================================================================
# _compat
# ===========================================================================

USING_REAL_GATEWAY: bool

try:  # pragma: no cover - exercised under a live Hermes install
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import SessionSource  # type: ignore
    from gateway.config import Platform  # type: ignore

    USING_REAL_GATEWAY = True
except Exception:  # pragma: no cover
    USING_REAL_GATEWAY = False

    class _PlatformMeta(type):
        _cache: dict[str, "Platform"] = {}

        def __call__(cls, value: str) -> "Platform":  # type: ignore[override]
            key = (value or "").strip().lower()
            if not key:
                raise ValueError("Platform value must be non-empty")
            cached = cls._cache.get(key)
            if cached is not None:
                return cached
            obj = super().__call__(key)
            cls._cache[key] = obj
            return obj

    class Platform(metaclass=_PlatformMeta):  # type: ignore[no-redef]
        """Minimal stub mirroring ``gateway.config.Platform``."""

        __slots__ = ("value",)

        def __init__(self, value: str) -> None:
            self.value = value

        def __repr__(self) -> str:
            return f"Platform({self.value!r})"

        def __eq__(self, other: object) -> bool:
            if isinstance(other, Platform):
                return self.value == other.value
            return NotImplemented

        def __hash__(self) -> int:
            return hash(("Platform", self.value))

    class MessageType(Enum):  # type: ignore[no-redef]
        TEXT = "text"
        LOCATION = "location"
        PHOTO = "photo"
        VIDEO = "video"
        AUDIO = "audio"
        VOICE = "voice"
        DOCUMENT = "document"
        STICKER = "sticker"
        COMMAND = "command"

    @dataclass
    class SessionSource:  # type: ignore[no-redef]
        platform: Platform
        chat_id: str
        chat_name: Optional[str] = None
        chat_type: str = "dm"
        user_id: Optional[str] = None
        user_name: Optional[str] = None
        thread_id: Optional[str] = None
        chat_topic: Optional[str] = None
        user_id_alt: Optional[str] = None
        chat_id_alt: Optional[str] = None
        is_bot: bool = False
        guild_id: Optional[str] = None
        parent_chat_id: Optional[str] = None
        message_id: Optional[str] = None

    @dataclass
    class MessageEvent:  # type: ignore[no-redef]
        text: str
        message_type: MessageType = MessageType.TEXT
        source: Optional[SessionSource] = None
        raw_message: Any = None
        message_id: Optional[str] = None
        platform_update_id: Optional[int] = None
        media_urls: list = field(default_factory=list)
        media_types: list = field(default_factory=list)
        reply_to_message_id: Optional[str] = None
        reply_to_text: Optional[str] = None
        auto_skill: Any = None
        channel_prompt: Optional[str] = None
        channel_context: Optional[str] = None
        internal: bool = False
        timestamp: datetime = field(default_factory=datetime.now)

    @dataclass
    class SendResult:  # type: ignore[no-redef]
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None
        retryable: bool = False
        continuation_message_ids: tuple = ()

    class _PlatformConfigStub:
        extra: dict = {}

    class BasePlatformAdapter:  # type: ignore[no-redef]
        """Test-only stub of the real ``BasePlatformAdapter``."""

        def __init__(self, config: Any = None, platform: Platform | None = None) -> None:
            self.config = config or _PlatformConfigStub()
            self.platform = platform or Platform("xmpp")
            self._message_handler: Callable[[MessageEvent], Awaitable[None]] | None = None

        def set_message_handler(
            self, handler: Callable[[MessageEvent], Awaitable[None]]
        ) -> None:
            self._message_handler = handler

        def build_source(
            self,
            chat_id: str,
            chat_name: Optional[str] = None,
            chat_type: str = "dm",
            user_id: Optional[str] = None,
            user_name: Optional[str] = None,
            thread_id: Optional[str] = None,
            chat_topic: Optional[str] = None,
            user_id_alt: Optional[str] = None,
            chat_id_alt: Optional[str] = None,
            is_bot: bool = False,
            guild_id: Optional[str] = None,
            parent_chat_id: Optional[str] = None,
            message_id: Optional[str] = None,
        ) -> SessionSource:
            return SessionSource(
                platform=self.platform,
                chat_id=str(chat_id),
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=str(user_id) if user_id else None,
                user_name=user_name,
                thread_id=str(thread_id) if thread_id else None,
                chat_topic=chat_topic.strip() if chat_topic else None,
                user_id_alt=user_id_alt,
                chat_id_alt=chat_id_alt,
                is_bot=is_bot,
                guild_id=str(guild_id) if guild_id else None,
                parent_chat_id=str(parent_chat_id) if parent_chat_id else None,
                message_id=str(message_id) if message_id else None,
            )

        async def handle_message(self, event: MessageEvent) -> None:
            if self._message_handler is None:
                return
            await self._message_handler(event)


# ===========================================================================
# _standalone
# ===========================================================================

_STANDALONE_RESOURCE_SUFFIX = "-cron"


def _build_client(cfg: XmppConfig):
    """Imported lazily so importing this module doesn't pull in slixmpp until
    the gateway actually constructs a sender."""
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


def _standalone_looks_like_room(cfg: XmppConfig, chat_id: str) -> bool:
    target = chat_id.lower()
    for room in cfg.muc_rooms:
        try:
            if parse_jid(room).bare.lower() == target:
                return True
        except ValueError:
            continue
    return False


async def standalone_sender_fn(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: Any = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Async entry point matching ``PlatformEntry.standalone_sender_fn``.

    XMPP has no native thread/media-document concept here — those kwargs
    are accepted for contract compatibility and ignored.
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
    mtype = "groupchat" if _standalone_looks_like_room(cfg, chat_id) else "chat"
    return await _send_once(cfg, chat_id, message, mtype=mtype)


# ===========================================================================
# adapter (XmppAdapter)
# ===========================================================================

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


_CHUNK_BYTES = 4096


def _split_utf8_chunk(data: bytes, limit: int) -> list[bytes]:
    """Slice ``data`` into ``<= limit`` byte chunks on UTF-8 boundaries."""
    out: list[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        end = min(i + limit, n)
        if end < n:
            while end > i and (data[end] & 0xC0) == 0x80:
                end -= 1
            if end == i:
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


_BACKOFF_SCHEDULE = (2, 4, 8, 16, 32)


class XmppAdapter(BasePlatformAdapter):
    """Hermes gateway adapter for XMPP."""

    def __init__(self, config: Any, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("xmpp"))
        extra = getattr(config, "extra", None) or {}
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
            "xep_0030",
            "xep_0045",
            "xep_0048",
            "xep_0066",
            "xep_0085",
            "xep_0199",
            "xep_0280",
            "xep_0313",
            "xep_0359",
            "xep_0363",
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
            await asyncio.sleep(0)

        last = message_ids[-1] if message_ids else None
        prefix = tuple(message_ids[:-1])
        return SendResult(success=True, message_id=last, continuation_message_ids=prefix)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
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
        self._connected_event.set()

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
        carbon_dir, inner = iter_carbon_payload(stanza)
        if carbon_dir == "sent":
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
            return
        if not self._is_allowed(sender_bare):
            log.info("XMPP plugin: denied DM from %s (not in XMPP_ALLOWED_JIDS)", sender_bare)
            return

        if replay and not self._replay_is_fresh(stanza):
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
            return

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
        if ts is None:
            ts = datetime.now(tz=timezone.utc).timestamp()
        if sid or ts is not None:
            self._last_seen.update(scope, stanza_id=sid, ts=ts)

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


PLATFORM_HINT = (
    "You are talking through XMPP. Use plain text — XHTML-IM and markdown "
    "tables are only rendered by some clients. Keep messages concise and "
    "avoid embedded HTML."
)


def adapter_factory(config: Any) -> XmppAdapter:
    """Hermes adapter factory — ``config`` is the gateway's PlatformConfig."""
    return XmppAdapter(config)


def interactive_setup() -> dict[str, str]:
    """CLI wizard invoked by ``hermes gateway setup`` when the user picks XMPP."""
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
    "standalone_sender_fn",
    "validate",
]

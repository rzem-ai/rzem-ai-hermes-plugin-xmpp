"""JID parsing helpers and session-key mapping for the XMPP gateway."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

try:
    from slixmpp.jid import JID as _SlixJID
except Exception:  # pragma: no cover - slixmpp is a runtime dep
    _SlixJID = None  # type: ignore[assignment]


PLATFORM = "xmpp"
CHAT_TYPE_PRIVATE = "private"
CHAT_TYPE_GROUP = "group"


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
    """Parse a JID string into its components. Uses slixmpp when available,
    falls back to a stdlib parser so unit tests don't need the library."""
    if not value or not isinstance(value, str):
        raise ValueError("JID must be a non-empty string")

    if _SlixJID is not None:
        jid = _SlixJID(value)
        bare = jid.bare
        if "@" not in bare:
            raise ValueError(f"JID missing local part: {value!r}")
        local, domain = bare.split("@", 1)
        return ParsedJID(bare=bare, local=local, domain=domain, resource=jid.resource or "")

    # Fallback parser (no slixmpp present): JID = [local@]domain[/resource]
    rest, _, resource = value.partition("/")
    if "@" not in rest:
        raise ValueError(f"JID missing local part: {value!r}")
    local, domain = rest.split("@", 1)
    if not local or not domain:
        raise ValueError(f"Malformed JID: {value!r}")
    return ParsedJID(bare=f"{local}@{domain}", local=local, domain=domain, resource=resource)


def bare_jid(value: str) -> str:
    return parse_jid(value).bare


def normalize_jid_set(values: Iterable[str]) -> set[str]:
    """Lower-case bare JIDs for case-insensitive membership checks."""
    out: set[str] = set()
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        out.add(parse_jid(v).bare.lower())
    return out


def build_session_key(chat_type: str, chat_id: str) -> str:
    """Local fallback for `gateway.session.build_session_key` — the live
    gateway is expected to canonicalize this when it actually runs.

    Format: ``agent:main:xmpp:{chat_type}:{chat_id}``
    """
    if chat_type not in (CHAT_TYPE_PRIVATE, CHAT_TYPE_GROUP):
        raise ValueError(f"Unknown chat_type: {chat_type!r}")
    return f"agent:main:{PLATFORM}:{chat_type}:{chat_id.lower()}"


def chat_id_for_dm(from_jid: str) -> tuple[str, str]:
    """Return (chat_type, chat_id) for a 1:1 message."""
    return CHAT_TYPE_PRIVATE, parse_jid(from_jid).bare.lower()


def chat_id_for_muc(room_jid: str) -> tuple[str, str]:
    """Return (chat_type, chat_id) for a group/MUC message."""
    return CHAT_TYPE_GROUP, parse_jid(room_jid).bare.lower()


def is_addressed_to_nick(body: str, nick: str) -> bool:
    """MUC addressing rule: message body must begin with the bot's nick,
    optionally followed by ``:``, ``,`` or whitespace."""
    if not body or not nick:
        return False
    stripped = body.lstrip()
    nick_low = nick.lower()
    head = stripped[: len(nick)].lower()
    if head != nick_low:
        return False
    tail = stripped[len(nick) :]
    if not tail:
        return True
    return tail[0] in (":", ",", " ", "\t")


def strip_nick_prefix(body: str, nick: str) -> str:
    """If the body begins with ``nick:``/``nick,``/``nick `` strip that prefix."""
    if not is_addressed_to_nick(body, nick):
        return body
    stripped = body.lstrip()
    rest = stripped[len(nick) :]
    if rest and rest[0] in (":", ","):
        rest = rest[1:]
    return rest.lstrip()

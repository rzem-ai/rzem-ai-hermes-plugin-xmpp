"""JID parsing and MUC addressing helpers for the XMPP gateway.

The plugin no longer builds its own session keys — :func:`gateway.session.
build_session_key` is the single source of truth. The adapter feeds the
gateway a :class:`gateway.session.SessionSource` and lets the gateway
derive the key.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

try:
    from slixmpp.jid import JID as _SlixJID
except Exception:  # pragma: no cover - slixmpp is a runtime dep
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

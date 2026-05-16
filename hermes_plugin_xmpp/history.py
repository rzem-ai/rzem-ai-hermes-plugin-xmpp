"""Message Carbons (XEP-0280) and Message Archive Management (XEP-0313)
support for the XMPP gateway.

The bulk of the protocol work is delegated to slixmpp's built-in plugins.
This module is responsible for:

- Persisting per-JID (and per-MUC) "last seen" cursors so we know where
  to resume a MAM catch-up after a restart.
- Unwrapping carbon-forwarded stanzas.
- Deduplicating stanzas that arrive twice (carbon + MAM, or MAM
  catch-ups that overlap).
- Deciding, for each replayed stanza, whether it is still "fresh enough"
  to trigger a live agent reply, or should be ingested silently.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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
        ts = _safe_delay_ts(stanza) or time.time()
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


def _safe_delay_ts(stanza: Any) -> float | None:
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
    ``bot_jid`` and ``muc:<room_jid>``. The shape is intentionally simple
    so external tools / users can poke at it."""

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

    def update(self, scope: str | None, *, stanza_id: str | None, ts: float | None) -> None:
        slot = self._slot(scope)
        entry = dict(self._data.get(slot, {}))
        if stanza_id:
            entry["stanza_id"] = stanza_id
        if ts is not None:
            entry["ts"] = ts
            entry["ts_iso"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        if entry:
            self._data[slot] = entry
            self._save()


def replay_should_respond(stanza_ts: float | None, *, grace_seconds: int, now: float | None = None) -> bool:
    """A MAM-replayed stanza triggers a live agent reply only if it is
    within ``grace_seconds`` of "now". Otherwise it is ingested silently
    (the gateway still gets a chance to update memory)."""
    if stanza_ts is None:
        # No timestamp: be conservative and treat as fresh so we don't
        # silently drop in-flight messages.
        return True
    return (now or time.time()) - stanza_ts <= grace_seconds


def iter_carbon_payload(stanza: Any) -> tuple[str | None, Any]:
    """If ``stanza`` is a Message Carbon, return ``("received"|"sent",
    inner_stanza)``. Otherwise return ``(None, stanza)``.

    slixmpp's ElementBase lazily synthesizes substanza accessors, so
    ``stanza['carbon_received']`` is truthy even when no carbon element
    exists. The reliable check is membership (``'carbon_received' in
    stanza``)."""
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
    """Pull forwarded stanzas out of a MAM iterator. Each ``result`` is
    expected to expose ``result['mam_result']['forwarded']['stanza']`` per
    slixmpp's XEP-0313 plugin."""
    out: list[Any] = []
    for result in results:
        try:
            inner = result["mam_result"]["forwarded"]["stanza"]
        except Exception:
            inner = None
        if inner is not None:
            out.append(inner)
    return out

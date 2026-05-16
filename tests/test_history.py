import time
from pathlib import Path

from hermes_plugin_xmpp.history import (
    DedupLRU,
    LastSeenStore,
    StanzaKey,
    iter_carbon_payload,
    replay_should_respond,
)


class _FakeNode:
    """Minimal slixmpp-stanza-shaped dict-getter for tests."""

    def __init__(self, data=None):
        self._data = data or {}

    def __getitem__(self, key):
        val = self._data.get(key)
        if val is None:
            return _FakeNode({})  # so .get / nested access doesn't blow up
        if isinstance(val, dict):
            return _FakeNode(val)
        return val

    def get(self, key, default=None):
        v = self._data.get(key, default)
        return v

    def __contains__(self, key):
        return key in self._data


def test_dedup_lru_marks_once():
    lru = DedupLRU(max_size=3)
    k = StanzaKey(value="sid:abc")
    assert lru.mark(k) is True
    assert lru.mark(k) is False
    assert lru.mark(StanzaKey(value="sid:def")) is True


def test_dedup_lru_evicts_oldest():
    lru = DedupLRU(max_size=2)
    a, b, c = (StanzaKey(value=f"sid:{x}") for x in ("a", "b", "c"))
    lru.mark(a)
    lru.mark(b)
    lru.mark(c)  # evicts a
    assert len(lru) == 2
    assert lru.mark(a) is True  # a comes back as new


def test_stanza_key_prefers_xep_0359():
    stanza = _FakeNode({"stanza_id": {"id": "xep-id"}, "origin_id": {"id": "oid"}, "from": "x@y", "id": "raw"})
    key = StanzaKey.from_stanza(stanza)
    assert key.value == "sid:xep-id"


def test_stanza_key_falls_back_to_origin_id():
    stanza = _FakeNode({"origin_id": {"id": "oid"}, "from": "x@y", "id": "raw"})
    key = StanzaKey.from_stanza(stanza)
    assert key.value == "oid:oid"


def test_stanza_key_falls_back_to_raw_tuple():
    stanza = _FakeNode({"from": "x@y", "id": "raw"})
    key = StanzaKey.from_stanza(stanza)
    assert key.value.startswith("raw:x@y|raw|")


def test_iter_carbon_payload_received():
    inner = _FakeNode({"body": "hello", "from": "alice@example.com"})
    stanza = _FakeNode({"carbon_received": {"forwarded": {"stanza": inner}}})
    direction, target = iter_carbon_payload(stanza)
    assert direction == "received"
    assert target is inner


def test_iter_carbon_payload_passthrough():
    stanza = _FakeNode({"body": "no carbon here"})
    direction, target = iter_carbon_payload(stanza)
    assert direction is None
    assert target is stanza


def test_replay_should_respond_fresh():
    now = time.time()
    assert replay_should_respond(now - 10, grace_seconds=60, now=now) is True


def test_replay_should_respond_stale():
    now = time.time()
    assert replay_should_respond(now - 600, grace_seconds=60, now=now) is False


def test_replay_should_respond_no_ts_defaults_fresh():
    assert replay_should_respond(None, grace_seconds=60) is True


def test_last_seen_store_roundtrip(tmp_path: Path):
    state = tmp_path / "seen.json"
    s1 = LastSeenStore("bot@example.com", path=state)
    s1.update(scope=None, stanza_id="sid-1", ts=1700000000.0)
    s1.update(scope="muc:room@conf", stanza_id="sid-2", ts=1700000100.0)

    s2 = LastSeenStore("bot@example.com", path=state)
    assert s2.get() == s1.get()
    assert s2.get(scope="muc:room@conf")["stanza_id"] == "sid-2"
    # Per-JID isolation:
    other = LastSeenStore("other@example.com", path=state)
    assert other.get() == {}


def test_last_seen_store_handles_corrupt_file(tmp_path: Path):
    state = tmp_path / "seen.json"
    state.write_text("not json", encoding="utf-8")
    s = LastSeenStore("bot@example.com", path=state)
    assert s.get() == {}
    # And it should overwrite cleanly on next update:
    s.update(scope=None, stanza_id="sid", ts=1.0)
    assert "sid" in state.read_text(encoding="utf-8")

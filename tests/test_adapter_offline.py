"""Adapter dispatch tests that do not touch the network.

These exercise the message-handling pipeline by feeding a hand-crafted
fake stanza into the adapter and checking what reaches the registered
gateway handler. The real ``slixmpp`` client is never constructed.
"""

import asyncio

import pytest

from hermes_plugin_xmpp._compat import MessageEvent, MessageType
from hermes_plugin_xmpp.adapter import (
    XmppAdapter,
    _chunk,
    _strip_markdown,
)
from hermes_plugin_xmpp.config import XmppConfig


def _make_adapter(**overrides) -> XmppAdapter:
    base = {
        "jid": "bot@example.com",
        "password": "pw",
        "allowed_jids": {"me@example.com"},
        "muc_rooms": ["team@conf.example.com"],
        "muc_nickname": "hermes-bot",
    }
    base.update(overrides)
    cfg = XmppConfig(**base)
    return XmppAdapter(cfg)


class _FakeStanza:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        v = self._data.get(key)
        if v is None:
            return _FakeStanza({})
        if isinstance(v, dict):
            return _FakeStanza(v)
        return v

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data


# ── pure helpers ──────────────────────────────────────────────────────────


def test_strip_markdown_basics():
    assert _strip_markdown("**bold** and *italic*") == "bold and italic"
    assert _strip_markdown("see [docs](https://x.test)") == "see docs (https://x.test)"
    assert _strip_markdown("`code` matters") == "code matters"
    assert _strip_markdown("# heading\nbody") == "heading\nbody"
    assert _strip_markdown("```py\nx = 1\n```") == "x = 1"


def test_chunk_short():
    assert _chunk("hello") == ["hello"]


def test_chunk_splits_on_lines():
    text = ("line " * 50 + "\n") * 200  # well over 4 KiB
    chunks = _chunk(text)
    assert len(chunks) > 1
    assert all(len(c.encode("utf-8")) <= 4096 for c in chunks)
    assert "".join(chunks).startswith("line line")


def test_chunk_splits_giant_single_line():
    text = "x" * 9000
    chunks = _chunk(text)
    assert len(chunks) >= 3
    assert "".join(chunks) == text


def test_chunk_preserves_multibyte_codepoints():
    # 3-byte CJK char; 9000 bytes worth would naively split a codepoint.
    text = "漢" * 3000
    chunks = _chunk(text, limit=1024)
    assert "".join(chunks) == text
    # Every chunk must decode round-trip without losing characters.
    for c in chunks:
        assert c.encode("utf-8").decode("utf-8") == c


# ── dispatch ──────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _capture(adapter):
    received: list[MessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    return received


def test_dispatch_dm_routes_to_handler():
    adapter = _make_adapter()
    received = _capture(adapter)

    stanza = _FakeStanza({
        "body": "hello bot",
        "from": "me@example.com/laptop",
        "id": "stanza-1",
        "stanza_id": {"id": "sid-1"},
    })
    _run(adapter._dispatch_dm(stanza, replay=False))

    assert len(received) == 1
    event = received[0]
    assert isinstance(event, MessageEvent)
    assert event.message_type == MessageType.TEXT
    assert event.text == "hello bot"
    assert event.message_id == "stanza-1"
    src = event.source
    assert src is not None
    assert src.platform.value == "xmpp"
    assert src.chat_type == "dm"
    assert src.chat_id == "me@example.com"
    assert src.user_id == "me@example.com"


def test_dispatch_dm_blocks_unauthorized_sender():
    adapter = _make_adapter()
    received = _capture(adapter)

    stanza = _FakeStanza({
        "body": "let me in",
        "from": "stranger@example.com/x",
        "id": "stanza-2",
        "stanza_id": {"id": "sid-2"},
    })
    _run(adapter._dispatch_dm(stanza, replay=False))
    assert received == []


def test_dispatch_dm_dedupes():
    adapter = _make_adapter()
    received = _capture(adapter)

    stanza = _FakeStanza({
        "body": "hi",
        "from": "me@example.com/laptop",
        "id": "stanza-3",
        "stanza_id": {"id": "sid-3"},
    })
    _run(adapter._dispatch_dm(stanza, replay=False))
    _run(adapter._dispatch_dm(stanza, replay=False))
    assert len(received) == 1


def test_dispatch_dm_ignores_own_bare_jid():
    adapter = _make_adapter()
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "self",
        "from": "bot@example.com/other",
        "id": "self-1",
        "stanza_id": {"id": "self-sid"},
    })
    _run(adapter._dispatch_dm(stanza, replay=False))
    assert received == []


def test_dispatch_muc_requires_addressing():
    adapter = _make_adapter()
    received = _capture(adapter)

    stanza = _FakeStanza({
        "body": "general chatter",
        "from": "team@conf.example.com/alice",
        "id": "muc-1",
        "stanza_id": {"id": "muc-sid-1"},
    })
    _run(adapter._dispatch_muc(stanza, replay=False))
    assert received == []

    stanza2 = _FakeStanza({
        "body": "hermes-bot: status?",
        "from": "team@conf.example.com/alice",
        "id": "muc-2",
        "stanza_id": {"id": "muc-sid-2"},
    })
    _run(adapter._dispatch_muc(stanza2, replay=False))
    assert len(received) == 1
    event = received[0]
    assert event.text == "status?"
    assert event.source.chat_type == "group"
    assert event.source.chat_id == "team@conf.example.com"
    assert event.source.user_id == "team@conf.example.com/alice"
    assert event.source.user_name == "alice"


def test_dispatch_muc_accepts_at_nick_mention():
    adapter = _make_adapter()
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "@hermes-bot: ping",
        "from": "team@conf.example.com/bob",
        "id": "muc-at",
        "stanza_id": {"id": "muc-at-sid"},
    })
    _run(adapter._dispatch_muc(stanza, replay=False))
    assert len(received) == 1
    assert received[0].text == "ping"


def test_dispatch_muc_accepts_xep_0461_reply_to_bot():
    adapter = _make_adapter()
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "no prefix, just a reply",
        "from": "team@conf.example.com/carol",
        "id": "muc-reply",
        "stanza_id": {"id": "muc-reply-sid"},
        "reply": {"to": "bot@example.com"},
    })
    _run(adapter._dispatch_muc(stanza, replay=False))
    assert len(received) == 1
    # Reply path keeps the original body — no nick prefix to strip.
    assert received[0].text == "no prefix, just a reply"


def test_dispatch_muc_ignores_own_echo():
    adapter = _make_adapter()
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "hermes-bot: hi all",
        "from": "team@conf.example.com/hermes-bot",
        "id": "muc-self",
        "stanza_id": {"id": "muc-self-sid"},
    })
    _run(adapter._dispatch_muc(stanza, replay=False))
    assert received == []


def test_dispatch_dm_carbon_sent_is_swallowed():
    adapter = _make_adapter()
    received = _capture(adapter)
    inner = _FakeStanza({
        "body": "I sent this from my phone",
        "from": "bot@example.com/phone",
        "id": "inner-1",
        "stanza_id": {"id": "inner-sid-1"},
    })
    wrapper = _FakeStanza({"carbon_sent": {"forwarded": {"stanza": inner}}})
    _run(adapter._on_message(wrapper))
    assert received == []


def test_replay_inside_grace_delivers():
    adapter = _make_adapter(mam_replay_grace_seconds=86_400 * 365 * 50)
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "recent enough",
        "from": "me@example.com/x",
        "id": "fresh-1",
        "stanza_id": {"id": "fresh-sid-1"},
        # Future-proof: 2030-01-01 is within the absurdly large grace window.
        "delay": {"stamp": "2030-01-01T00:00:00+00:00"},
    })
    _run(adapter._dispatch_dm(stanza, replay=True))
    assert len(received) == 1


def test_replay_outside_grace_is_dropped():
    """Stale MAM replays must NOT reach the gateway — restart-time history
    should never trigger a live reply (or a cross-platform mirror push).
    """
    adapter = _make_adapter(mam_replay_grace_seconds=1)
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "ancient",
        "from": "me@example.com/x",
        "id": "old-1",
        "stanza_id": {"id": "old-sid-1"},
        "delay": {"stamp": "2000-01-01T00:00:00+00:00"},
    })
    _run(adapter._dispatch_dm(stanza, replay=True))
    assert received == []


def test_replay_without_timestamp_is_dropped():
    """An undatable MAM stanza is treated as stale, not as fresh."""
    adapter = _make_adapter(mam_replay_grace_seconds=300)
    received = _capture(adapter)
    stanza = _FakeStanza({
        "body": "undatable",
        "from": "me@example.com/x",
        "id": "nodate-1",
        "stanza_id": {"id": "nodate-sid-1"},
    })
    _run(adapter._dispatch_dm(stanza, replay=True))
    assert received == []


@pytest.mark.parametrize("body", ["", "   ", "\n\n"])
def test_empty_body_is_dropped(body):
    adapter = _make_adapter()
    received = _capture(adapter)
    stanza = _FakeStanza({"body": body, "from": "me@example.com/x", "id": "empty"})
    _run(adapter._dispatch_dm(stanza, replay=False))
    assert received == []

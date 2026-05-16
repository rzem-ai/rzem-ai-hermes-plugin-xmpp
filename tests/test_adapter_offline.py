"""Adapter dispatch tests that do not touch the network.

These exercise the message-handling pipeline by feeding a hand-crafted
fake stanza into the adapter and checking what the gateway-side message
handler receives. The real ``slixmpp`` client is never constructed.
"""

import asyncio

import pytest

from hermes_plugin_xmpp.adapter import (
    XmppAdapter,
    XmppMessageEvent,
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
    # Round-trip
    assert "".join(chunks).startswith("line line")


def test_chunk_splits_giant_single_line():
    text = "x" * 9000
    chunks = _chunk(text)
    assert len(chunks) >= 3
    assert "".join(chunks) == text


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_dispatch_dm_routes_to_handler():
    adapter = _make_adapter()
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

    stanza = _FakeStanza({
        "body": "hello bot",
        "from": "me@example.com/laptop",
        "id": "stanza-1",
        "stanza_id": {"id": "sid-1"},
    })
    _run(adapter._dispatch_dm(stanza, replay=False))

    assert len(received) == 1
    event = received[0]
    assert event.chat_type == "private"
    assert event.chat_id == "me@example.com"
    assert event.session_key == "agent:main:xmpp:private:me@example.com"
    assert event.text == "hello bot"
    assert event.is_muc is False
    assert event.replay is False


def test_dispatch_dm_blocks_unauthorized_sender():
    adapter = _make_adapter()
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

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
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

    stanza = _FakeStanza({
        "body": "hi",
        "from": "me@example.com/laptop",
        "id": "stanza-3",
        "stanza_id": {"id": "sid-3"},
    })
    _run(adapter._dispatch_dm(stanza, replay=False))
    _run(adapter._dispatch_dm(stanza, replay=False))
    assert len(received) == 1


def test_dispatch_muc_requires_addressing():
    adapter = _make_adapter()
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

    # Unaddressed message — ignored.
    stanza = _FakeStanza({
        "body": "general chatter",
        "from": "team@conf.example.com/alice",
        "id": "muc-1",
        "stanza_id": {"id": "muc-sid-1"},
    })
    _run(adapter._dispatch_muc(stanza, replay=False))
    assert received == []

    # Addressed — accepted.
    stanza2 = _FakeStanza({
        "body": "hermes-bot: status?",
        "from": "team@conf.example.com/alice",
        "id": "muc-2",
        "stanza_id": {"id": "muc-sid-2"},
    })
    _run(adapter._dispatch_muc(stanza2, replay=False))
    assert len(received) == 1
    assert received[0].chat_type == "group"
    assert received[0].chat_id == "team@conf.example.com"
    assert received[0].text == "status?"
    assert received[0].is_muc is True


def test_dispatch_muc_ignores_own_echo():
    adapter = _make_adapter()
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

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
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

    inner = _FakeStanza({
        "body": "I sent this from my phone",
        "from": "bot@example.com/phone",
        "id": "inner-1",
        "stanza_id": {"id": "inner-sid-1"},
    })
    wrapper = _FakeStanza({"carbon_sent": {"forwarded": {"stanza": inner}}})
    _run(adapter._on_message(wrapper))
    assert received == []


def test_replay_outside_grace_marks_silent(monkeypatch):
    adapter = _make_adapter(mam_replay_grace_seconds=1)
    received: list[XmppMessageEvent] = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)

    # Stanza with an old XEP-0203 delay timestamp.
    stanza = _FakeStanza({
        "body": "old message",
        "from": "me@example.com/x",
        "id": "old-1",
        "stanza_id": {"id": "old-sid-1"},
        "delay": {"stamp": "2000-01-01T00:00:00+00:00"},
    })
    _run(adapter._dispatch_dm(stanza, replay=True))
    assert len(received) == 1
    assert received[0].extra.get("silent") is True


@pytest.mark.parametrize("body", ["", "   ", "\n\n"])
def test_empty_body_is_dropped(body):
    adapter = _make_adapter()
    received = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    stanza = _FakeStanza({"body": body, "from": "me@example.com/x", "id": "empty"})
    asyncio.run(adapter._dispatch_dm(stanza, replay=False))
    assert received == []


def test_register_returns_expected_shape():
    from hermes_plugin_xmpp.adapter import register

    plug = register()
    for key in (
        "name", "label", "kind", "factory", "is_configured", "validate",
        "interactive_setup", "env_enable_hook", "standalone_sender", "platform_hint",
    ):
        assert key in plug, f"register() missing key {key}"
    assert plug["name"] == "xmpp"
    assert plug["kind"] == "platform"

import pytest

from hermes_plugin_xmpp.jid_utils import (
    build_session_key,
    chat_id_for_dm,
    chat_id_for_muc,
    is_addressed_to_nick,
    normalize_jid_set,
    parse_jid,
    strip_nick_prefix,
)


def test_parse_jid_full():
    j = parse_jid("hermes@example.com/desktop")
    assert j.bare == "hermes@example.com"
    assert j.local == "hermes"
    assert j.domain == "example.com"
    assert j.resource == "desktop"
    assert j.full == "hermes@example.com/desktop"


def test_parse_jid_bare():
    j = parse_jid("me@example.com")
    assert j.bare == "me@example.com"
    assert j.resource == ""
    assert j.full == "me@example.com"


@pytest.mark.parametrize("bad", ["", "no-at-sign", "@no-local", "user@"])
def test_parse_jid_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_jid(bad)


def test_normalize_jid_set_lowercases_and_dedups():
    out = normalize_jid_set(["Alice@Ex.com", "alice@ex.com", "  ", "bob@ex.com"])
    assert out == {"alice@ex.com", "bob@ex.com"}


def test_session_key_format():
    ct, cid = chat_id_for_dm("Me@Example.com/phone")
    assert ct == "private"
    assert cid == "me@example.com"
    assert build_session_key(ct, cid) == "agent:main:xmpp:private:me@example.com"


def test_session_key_muc():
    ct, cid = chat_id_for_muc("team@conference.example.com")
    assert ct == "group"
    assert cid == "team@conference.example.com"
    assert build_session_key(ct, cid) == "agent:main:xmpp:group:team@conference.example.com"


def test_build_session_key_rejects_unknown_chat_type():
    with pytest.raises(ValueError):
        build_session_key("channel", "x@y")


@pytest.mark.parametrize(
    "body,expected",
    [
        ("bot: hello", True),
        ("Bot, hello", True),
        ("bot hello", True),
        ("not the bot", False),
        ("", False),
        ("botanist: hi", False),  # would-be prefix match is rejected
    ],
)
def test_is_addressed_to_nick(body, expected):
    assert is_addressed_to_nick(body, "bot") is expected


def test_strip_nick_prefix_variants():
    assert strip_nick_prefix("bot: hello", "bot") == "hello"
    assert strip_nick_prefix("Bot, hello", "bot") == "hello"
    assert strip_nick_prefix("bot   hello", "bot") == "hello"
    assert strip_nick_prefix("hello", "bot") == "hello"

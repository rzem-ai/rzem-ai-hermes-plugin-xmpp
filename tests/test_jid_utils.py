import pytest

from hermes_plugin_xmpp.jid_utils import (
    bare_jid,
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


def test_bare_jid_is_case_folded():
    # Whether slixmpp is present or not, bare_jid must be lower-case so
    # allowlist lookups don't depend on how the user typed the JID.
    assert bare_jid("Me@Example.com/Phone") == "me@example.com"


def test_normalize_jid_set_lowercases_and_dedups():
    out = normalize_jid_set(["Alice@Ex.com", "alice@ex.com", "  ", "bob@ex.com"])
    assert out == {"alice@ex.com", "bob@ex.com"}


@pytest.mark.parametrize(
    "body,expected",
    [
        ("bot: hello", True),
        ("Bot, hello", True),
        ("bot hello", True),
        ("@bot: hello", True),
        ("@bot hello", True),
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
    assert strip_nick_prefix("@bot: hello", "bot") == "hello"
    assert strip_nick_prefix("hello", "bot") == "hello"

"""Unit tests for the pub/sub registry, per-client channel tracker, and
Redis-style glob matcher.

`_match_pattern` is pure; `ChannelRegistry` is tested on fresh instances to
keep state isolated. The `ClientChannels` tests register against the
module-level `channel_registry` singleton (matching production behavior),
so an autouse fixture clears it before/after each test.
"""
from unittest.mock import MagicMock

import pytest

from miniredis.client import ClientState
from miniredis.pubsub import ChannelRegistry, channel_registry


BS = bytes([0x5C])   # literal backslash; avoids string-escape footguns in test data


def _mock_client() -> ClientState:
    """Fresh ClientState with a mock writer (write path not exercised here)."""
    return ClientState(MagicMock())


# ---------------------------------------------------------------------------
# Pattern matcher
# ---------------------------------------------------------------------------


class TestMatchPatternLiterals:
    @pytest.mark.parametrize("pattern,string,expected", [
        (b"", b"", True),
        (b"foo", b"foo", True),
        (b"foo", b"bar", False),
        (b"foo", b"fo", False),
        (b"foo", b"fooo", False),
        # Byte-level, case-sensitive
        (b"Foo", b"foo", False),
        # Arbitrary bytes (non-UTF-8)
        (bytes([0xFF, 0x80]), bytes([0xFF, 0x80]), True),
        (bytes([0xFF, 0x80]), bytes([0xFF, 0x81]), False),
    ])
    def test_literal_match(self, pattern, string, expected):
        assert ChannelRegistry()._match_pattern(pattern, string) is expected


class TestMatchPatternStar:
    @pytest.mark.parametrize("pattern,string,expected", [
        (b"*", b"", True),
        (b"*", b"anything", True),
        (b"foo*", b"foobar", True),
        (b"foo*", b"foo", True),                   # trailing * vs exhausted string
        (b"*foo", b"xxfoo", True),
        (b"*foo", b"xxfooX", False),               # full-match: trailing X rejects
        (b"a*b*c", b"aXXbYYc", True),              # backtracking across two stars
        (b"a*b*c", b"abc", True),
        (b"a*b*c", b"abd", False),
        (b"**foo", b"xfoo", True),                 # collapse consecutive *
        (b"foo***", b"foo", True),                 # multiple trailing stars
    ])
    def test_star_match(self, pattern, string, expected):
        assert ChannelRegistry()._match_pattern(pattern, string) is expected


class TestMatchPatternQuestionMark:
    @pytest.mark.parametrize("pattern,string,expected", [
        (b"f?o", b"foo", True),
        (b"f?o", b"fxo", True),
        (b"f?o", b"fo", False),                    # ? must match exactly one
        (b"f?o", b"fxxo", False),
        (b"???", b"abc", True),
        (b"???", b"ab", False),
    ])
    def test_question_mark_match(self, pattern, string, expected):
        assert ChannelRegistry()._match_pattern(pattern, string) is expected


class TestMatchPatternCharacterClass:
    @pytest.mark.parametrize("pattern,string,expected", [
        # Positive class
        (b"[abc]", b"a", True),
        (b"[abc]", b"b", True),
        (b"[abc]", b"c", True),
        (b"[abc]", b"d", False),
        # Negation (Redis uses ^, NOT !)
        (b"[^abc]", b"d", True),
        (b"[^abc]", b"a", False),
        # `!` is literal inside a class (different from fnmatch)
        (b"[!abc]", b"!", True),
        (b"[!abc]", b"a", True),
        (b"[!abc]", b"d", False),
        # Byte ranges with endpoint swap
        (b"[a-z]", b"m", True),
        (b"[a-z]", b"M", False),                   # case-sensitive
        (b"[A-Z]", b"M", True),
        (b"[0-9]", b"5", True),
        (b"[0-9]", b"a", False),
        (b"[z-a]", b"m", True),                    # reversed range auto-swaps
        # Multiple ranges + literals
        (b"[a-zA-Z_]", b"_", True),
        (b"[a-zA-Z_]", b"5", False),
        # Empty / pathological classes
        (b"[]", b"a", False),                      # empty class matches nothing
        (b"[^]", b"a", True),                      # empty negated matches anything
        (b"[^]", bytes([0x00]), True),
        # Unclosed `[`
        (b"[abc", b"a", False),
        (b"[abc", b"[abc", False),
    ])
    def test_character_class(self, pattern, string, expected):
        assert ChannelRegistry()._match_pattern(pattern, string) is expected


class TestMatchPatternEscapes:
    @pytest.mark.parametrize("pattern,string,expected", [
        # Top-level escapes (literal special chars)
        (b"foo" + BS + b"*", b"foo*", True),
        (b"foo" + BS + b"*", b"foobar", False),
        (b"foo" + BS + b"?", b"foo?", True),
        (b"foo" + BS + b"?", b"foox", False),
        (b"foo" + BS + b"[", b"foo[", True),
        (b"foo" + BS + BS, b"foo" + BS, True),
        # Lone trailing backslash is literal '\'
        (b"foo" + BS, b"foo" + BS, True),
        (b"foo" + BS, b"foo", False),
        # \<letter> is literal letter (NOT a regex character class)
        (b"foo" + BS + b"d", b"food", True),
        (b"foo" + BS + b"d", b"foo5", False),      # no \d "digit" semantics
        # Escapes inside character class
        (b"[" + BS + b"]]", b"]", True),           # literal ']' inside class
        (b"[" + BS + b"-]", b"-", True),           # literal '-' inside class
        (b"[" + BS + BS + b"]", BS, True),         # literal '\' inside class
    ])
    def test_escape(self, pattern, string, expected):
        assert ChannelRegistry()._match_pattern(pattern, string) is expected


class TestMatchPatternRealistic:
    @pytest.mark.parametrize("pattern,string,expected", [
        (b"user.*.events", b"user.42.events", True),
        (b"user.*.events", b"user.42.commands", False),
        (b"news.[abc]*", b"news.alerts", True),
        (b"news.[abc]*", b"news.zlerts", False),
        (b"chat:?:room:*", b"chat:1:room:lobby", True),
        (b"chat:?:room:*", b"chat::room:lobby", False),    # ? requires exactly 1
        # The "match everything" pattern
        (b"*", b"any.channel.name", True),
        (b"*", b"", True),
    ])
    def test_realistic_channels(self, pattern, string, expected):
        assert ChannelRegistry()._match_pattern(pattern, string) is expected


# ---------------------------------------------------------------------------
# ChannelRegistry -- fresh instance per test, no global state.
# ---------------------------------------------------------------------------


class TestChannelRegistry:
    def test_empty_registry_has_no_subscribers(self):
        r = ChannelRegistry()
        assert r.channel_subs == {}
        assert r.pchannel_subs == {}

    def test_subscribe_adds_client_to_channel(self):
        r = ChannelRegistry()
        c = _mock_client()
        r.subscribe(b"news", c)
        assert b"news" in r.channel_subs
        assert c in r.channel_subs[b"news"]

    def test_subscribe_same_client_twice_is_idempotent(self):
        r = ChannelRegistry()
        c = _mock_client()
        r.subscribe(b"news", c)
        r.subscribe(b"news", c)
        assert len(r.channel_subs[b"news"]) == 1

    def test_multiple_clients_subscribe_to_same_channel(self):
        r = ChannelRegistry()
        a, b = _mock_client(), _mock_client()
        r.subscribe(b"news", a)
        r.subscribe(b"news", b)
        assert {a, b} == r.channel_subs[b"news"]

    def test_one_client_subscribed_to_multiple_channels(self):
        r = ChannelRegistry()
        c = _mock_client()
        r.subscribe(b"news", c)
        r.subscribe(b"alerts", c)
        assert c in r.channel_subs[b"news"]
        assert c in r.channel_subs[b"alerts"]

    def test_unsubscribe_removes_client(self):
        r = ChannelRegistry()
        c = _mock_client()
        r.subscribe(b"news", c)
        r.unsubscribe(b"news", c)
        # When the last subscriber leaves, the channel itself is removed.
        assert b"news" not in r.channel_subs

    def test_unsubscribe_keeps_channel_if_other_subscribers_remain(self):
        r = ChannelRegistry()
        a, b = _mock_client(), _mock_client()
        r.subscribe(b"news", a)
        r.subscribe(b"news", b)
        r.unsubscribe(b"news", a)
        assert r.channel_subs[b"news"] == {b}

    def test_unsubscribe_unknown_channel_is_noop(self):
        # Regression: defaultdict used to lazily create empty entries here.
        r = ChannelRegistry()
        r.unsubscribe(b"nope", _mock_client())
        assert r.channel_subs == {}

    def test_unsubscribe_client_not_subscribed_is_noop(self):
        r = ChannelRegistry()
        a, b = _mock_client(), _mock_client()
        r.subscribe(b"news", a)
        r.unsubscribe(b"news", b)    # b was never subscribed
        assert r.channel_subs[b"news"] == {a}

    def test_publish_to_channel_with_no_subscribers_returns_zero(self):
        # Regression: KeyError used to fire here before .get(default=set()).
        r = ChannelRegistry()
        assert r.publish(b"empty", b"hi") == 0

    def test_publish_to_direct_subscribers_writes_message_frame_to_each(self):
        r = ChannelRegistry()
        a, b = _mock_client(), _mock_client()
        r.subscribe(b"news", a)
        r.subscribe(b"news", b)

        count = r.publish(b"news", b"hello")
        assert count == 2
        # Each subscriber received one frame.
        a_msg = a.write_buffer.get_nowait()
        b_msg = b.write_buffer.get_nowait()
        assert a_msg == b_msg
        # Verify it's an array of three bulk strings: ["message", channel, payload]
        assert b"message" in a_msg
        assert b"news" in a_msg
        assert b"hello" in a_msg

    def test_psubscribe_registers_pattern(self):
        r = ChannelRegistry()
        c = _mock_client()
        r.psubscribe(b"news.*", c)
        assert b"news.*" in r.pchannel_subs
        assert c in r.pchannel_subs[b"news.*"]

    def test_publish_delivers_to_matching_pattern_subscribers(self):
        r = ChannelRegistry()
        a = _mock_client()
        r.psubscribe(b"news.*", a)

        count = r.publish(b"news.tech", b"story")
        assert count == 1
        msg = a.write_buffer.get_nowait()
        # pmessage frame: ["pmessage", pattern, channel, payload]
        assert b"pmessage" in msg
        assert b"news.*" in msg
        assert b"news.tech" in msg
        assert b"story" in msg

    def test_publish_does_not_deliver_to_non_matching_pattern(self):
        r = ChannelRegistry()
        a = _mock_client()
        r.psubscribe(b"news.*", a)
        count = r.publish(b"alerts.urgent", b"x")
        assert count == 0
        assert a.write_buffer.empty()

    def test_publish_count_sums_direct_and_pattern_receivers(self):
        r = ChannelRegistry()
        d, p = _mock_client(), _mock_client()
        r.subscribe(b"news.tech", d)
        r.psubscribe(b"news.*", p)
        # The direct subscriber and the pattern subscriber both receive once.
        count = r.publish(b"news.tech", b"story")
        assert count == 2
        # Each got exactly one frame (different shapes -- message vs pmessage).
        assert b"message" in d.write_buffer.get_nowait()
        assert b"pmessage" in p.write_buffer.get_nowait()

    def test_punsubscribe_removes_pattern(self):
        r = ChannelRegistry()
        c = _mock_client()
        r.psubscribe(b"news.*", c)
        r.punsubscribe(b"news.*", c)
        assert b"news.*" not in r.pchannel_subs


# ---------------------------------------------------------------------------
# ClientChannels -- uses the module-level singleton, autouse reset.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_global_registry():
    """Clear the global `channel_registry` before/after each test in this
    file's ClientChannels section (and below). Pubsub registry state must
    not bleed across tests.
    """
    channel_registry.channel_subs.clear()
    channel_registry.pchannel_subs.clear()
    yield
    channel_registry.channel_subs.clear()
    channel_registry.pchannel_subs.clear()


class TestClientChannels:
    def test_initial_state(self):
        c = _mock_client()
        assert c.channels.get_channel_count() == 0
        assert c.channels.channels == set()
        assert c.channels.pchannels == set()
        assert c.is_subscribed is False

    def test_sub_channel_updates_registry_and_per_client_state(self):
        c = _mock_client()
        c.channels.sub_channel([b"news"])
        # per-client view
        assert b"news" in c.channels.channels
        # global registry view
        assert c in channel_registry.channel_subs[b"news"]
        # is_subscribed flips on
        assert c.is_subscribed is True

    def test_sub_channel_writes_ack_to_buffer(self):
        c = _mock_client()
        c.channels.sub_channel([b"news"])
        ack = c.write_buffer.get_nowait()
        # 3-element array: subscribe, channel, count=1
        assert ack.startswith(b"*3\r\n")
        assert b"subscribe" in ack
        assert b"news" in ack
        assert b":1\r\n" in ack       # current count after subscribing

    def test_sub_channel_count_increments_per_added_channel(self):
        c = _mock_client()
        c.channels.sub_channel([b"a", b"b", b"c"])
        # Three frames, with running counts 1, 2, 3
        msgs = [c.write_buffer.get_nowait() for _ in range(3)]
        assert b":1\r\n" in msgs[0]
        assert b":2\r\n" in msgs[1]
        assert b":3\r\n" in msgs[2]

    def test_unsub_channel_with_empty_list_unsubs_all(self):
        # Regression: this used to crash with "set changed size during
        # iteration" because the code aliased `channel_names = self.channels`
        # and then mutated `self.channels` inside the loop.
        c = _mock_client()
        c.channels.sub_channel([b"a", b"b", b"c"])
        # drain the subscribe acks
        for _ in range(3):
            c.write_buffer.get_nowait()
        c.channels.unsub_channel([])
        assert c.channels.channels == set()
        assert c.is_subscribed is False
        # And the registry was cleaned up for each channel.
        assert b"a" not in channel_registry.channel_subs
        assert b"b" not in channel_registry.channel_subs
        assert b"c" not in channel_registry.channel_subs

    def test_unsub_pchannel_with_empty_list_unsubs_all(self):
        c = _mock_client()
        c.channels.sub_pattern([b"news.*", b"alerts.*"])
        for _ in range(2):
            c.write_buffer.get_nowait()
        c.channels.unsub_pchannel([])
        assert c.channels.pchannels == set()
        assert b"news.*" not in channel_registry.pchannel_subs
        assert b"alerts.*" not in channel_registry.pchannel_subs

    def test_unsub_channel_with_specific_names(self):
        c = _mock_client()
        c.channels.sub_channel([b"a", b"b", b"c"])
        for _ in range(3):
            c.write_buffer.get_nowait()
        c.channels.unsub_channel([b"a", b"c"])
        # `b` survives, `a` and `c` are gone.
        assert c.channels.channels == {b"b"}
        assert c.is_subscribed is True

    def test_get_channel_count_sums_channels_and_patterns(self):
        c = _mock_client()
        c.channels.sub_channel([b"a", b"b"])
        c.channels.sub_pattern([b"p.*"])
        assert c.channels.get_channel_count() == 3

    def test_is_subscribed_flips_off_when_last_channel_leaves(self):
        c = _mock_client()
        c.channels.sub_channel([b"x"])
        assert c.is_subscribed is True
        c.channels.unsub_channel([b"x"])
        assert c.is_subscribed is False

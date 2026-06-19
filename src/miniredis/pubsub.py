from __future__ import annotations
from typing import TYPE_CHECKING

from miniredis.protocol import encode_array, encode_bulk_string, encode_integer

if TYPE_CHECKING:
    from miniredis.client import ClientState


class ChannelRegistry:
    def __init__(self):
        self.channel_subs: dict[bytes, set[ClientState]] = {}
        self.pchannel_subs: dict[bytes, set[ClientState]] = {}
    
    def subscribe(self, channel_name: bytes, client: ClientState) -> None:
        self.channel_subs.setdefault(channel_name, set()).add(client)
    
    def psubscribe(self, channel_pattern: bytes, client: ClientState) -> None:
        self.pchannel_subs.setdefault(channel_pattern, set()).add(client)
    
    def unsubscribe(self, channel_name: bytes, client: ClientState) -> None:
        if self.channel_subs.get(channel_name):
            self.channel_subs[channel_name].discard(client)

            if not self.channel_subs.get(channel_name):
                del self.channel_subs[channel_name]
    
    def punsubscribe(self, channel_pattern: bytes, client: ClientState) -> None:
        if self.pchannel_subs.get(channel_pattern):
            self.pchannel_subs[channel_pattern].discard(client)

            if not self.pchannel_subs.get(channel_pattern):
                del self.pchannel_subs[channel_pattern]
    
    def _match_pattern(self, pattern: bytes, string: bytes) -> bool:
        """Redis-style glob match. Returns True iff `pattern` matches the
        entire `string` (no anchoring or partial matches).

        Operators (mirrors Redis's `stringmatchlen` exactly):

        Top-level:
            *           zero or more of any bytes
            ?           exactly one byte
            [...]       character class (see below)
            \\<byte>    escape: literal next byte (lone trailing `\\` is literal `\\`)
            any other   literal byte

        Inside `[...]`:
            ^ at start  negate the class
            a-z         byte range (endpoints swap if reversed)
            \\<byte>    escape: literal next byte
            ]           close the class
            any other   literal class member

        Empty class `[]` matches no byte; empty negated class `[^]` matches
        any byte. An unclosed `[` causes the match to fail. Trailing `*`s
        in the pattern after the string is exhausted are consumed cleanly.
        Byte-level comparisons; case-sensitive.
        """
        pn, sn = len(pattern), len(string)
        STAR, QMARK, LBR, RBR, BSL, DASH, CARET = (
            ord('*'), ord('?'), ord('['), ord(']'), ord('\\'), ord('-'), ord('^')
        )

        def match_class(pi: int, b: int) -> tuple[bool, int]:
            """Test byte `b` against the class starting at pattern[pi] == '['.

            Returns (matched, next_pi). On an unclosed class, returns
            (False, pn) so the outer loop fails the match cleanly.
            """
            pi += 1  # skip '['
            negate = pi < pn and pattern[pi] == CARET
            if negate:
                pi += 1
            matched = False
            closed = False
            while pi < pn:
                pc = pattern[pi]
                if pc == BSL and pi + 1 < pn:
                    # Escaped literal byte inside the class.
                    pi += 1
                    if pattern[pi] == b:
                        matched = True
                    pi += 1
                elif pc == RBR:
                    closed = True
                    pi += 1
                    break
                elif pi + 2 < pn and pattern[pi + 1] == DASH:
                    # Byte range "a-z".
                    lo, hi = pattern[pi], pattern[pi + 2]
                    if lo > hi:
                        lo, hi = hi, lo
                    if lo <= b <= hi:
                        matched = True
                    pi += 3
                else:
                    # Plain literal member of the class.
                    if pc == b:
                        matched = True
                    pi += 1
            if not closed:
                return False, pi
            if negate:
                matched = not matched
            return matched, pi

        def match(pi: int, si: int) -> bool:
            while pi < pn and si < sn:
                c = pattern[pi]
                if c == STAR:
                    # Collapse consecutive stars.
                    while pi + 1 < pn and pattern[pi + 1] == STAR:
                        pi += 1
                    # Trailing '*' matches the rest of the string unconditionally.
                    if pi + 1 == pn:
                        return True
                    # Try matching the rest of the pattern at every position.
                    for k in range(si, sn + 1):
                        if match(pi + 1, k):
                            return True
                    return False
                elif c == QMARK:
                    pi += 1
                    si += 1
                elif c == LBR:
                    ok, pi = match_class(pi, string[si])
                    if not ok:
                        return False
                    si += 1
                elif c == BSL:
                    # Escape: next byte is literal. Lone trailing '\' falls
                    # through and is matched as a literal '\'.
                    if pi + 1 < pn:
                        pi += 1
                    if pattern[pi] != string[si]:
                        return False
                    pi += 1
                    si += 1
                else:
                    if c != string[si]:
                        return False
                    pi += 1
                    si += 1
            # String exhausted: trailing '*'s in the pattern are still ok.
            while pi < pn and pattern[pi] == STAR:
                pi += 1
            return pi == pn and si == sn

        return match(0, 0)
    
    def publish(self, channel_name: bytes, message: bytes) -> int:
        receivers = self.channel_subs.get(channel_name, set())
        count = len(receivers)
        message_array = [
            encode_bulk_string(b"message"),
            encode_bulk_string(channel_name),
            encode_bulk_string(message)
        ]
        message_bytes = encode_array(message_array)

        for receiver in receivers:
            receiver.write_to_buffer(message_bytes) # When this blocks, will control goto next iter?

        for pattern, subs in self.pchannel_subs.items():
            if self._match_pattern(pattern, channel_name):
                count += len(subs)
                message_array = [
                    encode_bulk_string(b"pmessage"),
                    encode_bulk_string(pattern),
                    encode_bulk_string(channel_name),
                    encode_bulk_string(message)
                ]
                message_bytes = encode_array(message_array)
                for client in subs:
                    client.write_to_buffer(message_bytes)
        
        return count

class ClientChannels:
    def __init__(self, client):
        self.client: ClientState = client
        self.channels: set[bytes] = set()
        self.pchannels: set[bytes] = set()
    
    def get_channel_count(self) -> int:
        return len(self.channels) + len(self.pchannels)
    
    def _get_bytes_message(self, action: bytes, channel: bytes | None) -> bytes:
        message_array = [
            encode_bulk_string(action),
            encode_bulk_string(channel),
            encode_integer(self.get_channel_count())
        ]

        return encode_array(message_array)
    
    def sub_channel(self, channel_names: list[bytes]) -> None:
        for channel in channel_names:
            self.channels.add(channel)
            channel_registry.subscribe(channel, self.client)
            message = self._get_bytes_message(b"subscribe", channel)
            self.client.write_to_buffer(message)
    
    def sub_pattern(self, channel_patterns: list[bytes]) -> None:
        for pattern in channel_patterns:
            self.pchannels.add(pattern)
            channel_registry.psubscribe(pattern, self.client)
            message = self._get_bytes_message(b"psubscribe", pattern)
            self.client.write_to_buffer(message)
    
    def unsub_channel(self, channel_names: list[bytes]) -> None:
        if not channel_names:
            channel_names = list(self.channels)

        for channel in channel_names:
            self.channels.discard(channel)
            channel_registry.unsubscribe(channel, self.client)
            message = self._get_bytes_message(b"unsubscribe", channel)
            self.client.write_to_buffer(message)
    
    def unsub_pchannel(self, channel_patterns: list[bytes]) -> None:
        if not channel_patterns:
            channel_patterns = list(self.pchannels)

        for pattern in channel_patterns:
            self.pchannels.discard(pattern)
            channel_registry.punsubscribe(pattern, self.client)
            message = self._get_bytes_message(b"punsubscribe", pattern)
            self.client.write_to_buffer(message)

channel_registry = ChannelRegistry()

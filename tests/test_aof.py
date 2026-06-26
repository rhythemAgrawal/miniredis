"""AOF (append-only file) persistence, parser, and replay tests.

Covers four layers:
  1. The ``AOF`` class -- open/write/close lifecycle, per-policy fsync behavior.
  2. ``FileStreamReader`` + ``read_aof_command`` -- sync parser used in replay
     plus the tail-truncation offset tracking it relies on.
  3. ``append_to_aof`` -- routing writes to main and (when a snapshot is mid-
     flight) the temp AOF.
  4. ``replay_commands`` end-to-end -- driving the parser + replay_dispatch
     against a real on-disk AOF, including transaction envelopes, tail
     truncation repair, and mid-file corruption rejection.

The cached singletons (``get_main_aof``, ``get_temp_aof``) are reset between
tests via the ``aof_paths`` fixture; without that step a path-mutating test
would leak its AOF instance into the next test and silently write to the wrong
file.
"""
import io
import os
import time
from unittest.mock import MagicMock

import pytest

import miniredis.store as store_module
from miniredis.aof import (
    AOF,
    append_to_aof,
    get_main_aof,
    get_temp_aof,
)
from miniredis.client import ClientState
from miniredis.commands import dispatch, replay_dispatch
from miniredis.config import get_settings
from miniredis.custom_data_structures import RandomDict
from miniredis.protocol import (
    FileStreamReader,
    ProtocolError,
    read_aof_command,
    resp_encode_command,
)
from miniredis.server import replay_commands
from miniredis.store import store


# --- fixtures ----------------------------------------------------------


@pytest.fixture
def aof_paths(tmp_path, monkeypatch):
    """Per-test AOF paths + clean singleton cache.

    ``get_main_aof()`` / ``get_temp_aof()`` are ``@cache``d on no args, so a
    settings-only monkeypatch wouldn't take effect once the cache had been
    populated by an earlier test. We clear the cache on both ends so the
    next call builds a fresh AOF pointing at the per-test temp path.
    """
    main_path = tmp_path / "main.aof"
    temp_path = tmp_path / "temp.aof"
    settings = get_settings()
    monkeypatch.setattr(settings, "aof_main_file_path", str(main_path))
    monkeypatch.setattr(settings, "aof_temp_file_path", str(temp_path))
    get_main_aof.cache_clear()
    get_temp_aof.cache_clear()
    yield main_path, temp_path
    for getter in (get_main_aof, get_temp_aof):
        try:
            getter().close()
        except Exception:
            pass
    get_main_aof.cache_clear()
    get_temp_aof.cache_clear()
    store_module.snapshot_pid = None


@pytest.fixture(autouse=True)
def reset_store():
    """Same pattern as ``test_commands.py`` -- the singleton ``store`` is shared."""
    store._data.clear()
    store._ttl = RandomDict()
    yield
    store._data.clear()
    store._ttl = RandomDict()


def _aof(policy: str, path) -> AOF:
    """Build an AOF and override its fsync policy.

    ``AOF.__init__`` reads ``fsync_policy`` from global settings; rather than
    monkeypatch settings for every test we just set the attribute after
    construction. The only effect of init-time policy is the ``atexit`` hook,
    which we don't exercise in unit tests.
    """
    aof = AOF(str(path))
    aof.fsync_policy = policy
    return aof


def _make_reader(data: bytes) -> FileStreamReader:
    """Build a FileStreamReader over an in-memory bytes blob.

    ``BytesIO`` supports ``read()`` and ``tell()`` -- the only file methods
    FileStreamReader uses -- so it's a drop-in stand-in for an on-disk file.
    """
    return FileStreamReader(io.BytesIO(data))


def _dummy_client() -> ClientState:
    return ClientState(MagicMock())


async def _dispatch_and_append(argv, client) -> bytes | None:
    """Mirror what ``server.handle_request`` does on each command.

    ``dispatch`` returns ``(response, append_cmd)`` and explicitly leaves the
    AOF write to the caller -- EXEC is the one exception (it appends its
    own envelope internally). Tests that drive ``dispatch`` directly must
    do the append themselves or the AOF stays empty.
    """
    response, append_cmd = await dispatch(argv, client)
    if append_cmd:
        append_to_aof(append_cmd)
    return response


def _write_aof(path, *commands) -> None:
    """Pre-build an AOF file with the canonical RESP encoder.

    Each ``cmd`` is a list of bytes args including the command name at
    ``argv[0]``. This is the same encoding ``dispatch`` produces, so
    round-trips via this helper exercise the real wire format.
    """
    with open(path, "wb") as f:
        for cmd in commands:
            f.write(resp_encode_command(cmd))


# ============================ AOF class =============================


class TestAOFLifecycle:
    """Open/close lifecycle and the closed-handle guard."""

    def test_close_before_open_is_noop(self, tmp_path):
        # Regression: ``close()`` must not AttributeError if the AOF was
        # constructed but never opened -- this is the steady state of
        # ``temp_aof`` between snapshots.
        aof = AOF(str(tmp_path / "x.aof"))
        aof.close()

    def test_open_creates_the_file(self, tmp_path):
        path = tmp_path / "x.aof"
        aof = _aof("NO", path)
        assert not path.exists()
        aof.open()
        try:
            assert path.exists()
        finally:
            aof.close()

    def test_close_is_idempotent(self, tmp_path):
        aof = _aof("NO", tmp_path / "x.aof")
        aof.open()
        aof.close()
        aof.close()  # second close must not raise

    def test_open_in_append_mode_preserves_prior_content(self, tmp_path):
        # The append mode is critical for re-opening after a tail-truncation
        # repair: prior valid commands stay, new writes go to the end.
        path = tmp_path / "x.aof"
        path.write_bytes(b"prior\r\n")
        aof = _aof("NO", path)
        aof.open()
        try:
            aof.write(b"new\r\n")
        finally:
            aof.close()
        assert path.read_bytes() == b"prior\r\nnew\r\n"


class TestAOFWrite:
    def test_no_policy_writes_to_file(self, tmp_path):
        path = tmp_path / "x.aof"
        aof = _aof("NO", path)
        aof.open()
        try:
            aof.write(b"hello")
        finally:
            aof.close()
        assert path.read_bytes() == b"hello"

    def test_always_policy_fsyncs_after_each_write(self, tmp_path, monkeypatch):
        # Patch the ``os.fsync`` symbol the AOF module looked up at import
        # time; counting calls is enough to assert the policy is honored.
        import miniredis.aof as aof_module
        fsync_calls = []
        monkeypatch.setattr(aof_module.os, "fsync",
                            lambda fd: fsync_calls.append(fd))
        aof = _aof("ALWAYS", tmp_path / "x.aof")
        aof.open()
        try:
            aof.write(b"a")
            aof.write(b"b")
            # ALWAYS: one fsync per write. close() does one more.
            assert len(fsync_calls) >= 2
        finally:
            aof.close()

    def test_everysec_thread_starts_and_joins_on_close(self, tmp_path):
        # Structural test: opening with EVERYSEC must start the periodic
        # fsync thread (or the policy silently degrades to NO), and close
        # must signal + join it (or the daemon thread keeps running after
        # the file handle is gone).
        aof = _aof("EVERYSEC", tmp_path / "x.aof")
        aof.open()
        assert aof.thread.is_alive()
        assert aof.thread.daemon
        aof.close()
        assert not aof.thread.is_alive()

    def test_everysec_marks_dirty_on_write(self, tmp_path):
        # The dirty flag is what the periodic thread reads to decide whether
        # to fsync. If a write doesn't set it, the policy is a no-op even
        # with the thread running.
        aof = _aof("EVERYSEC", tmp_path / "x.aof")
        # Open with a generous interval so the thread doesn't race us to
        # clear the flag before we can assert on it.
        aof.open()
        try:
            # Force a long interval so the periodic thread doesn't race.
            aof.interval = 60
            aof.is_dirty = False
            aof.write(b"a")
            assert aof.is_dirty is True
        finally:
            aof.close()


# ========================== FileStreamReader =========================


class TestFileStreamReader:
    def test_read_exactly_returns_bytes_not_bytearray(self):
        # Regression: bytearray slicing returns bytearray, which is
        # unhashable and breaks ``COMMANDS.get(cmd_name)`` during replay.
        # The boundary conversion happens in read_exactly.
        r = _make_reader(b"hello world")
        out = r.read_exactly(5)
        assert isinstance(out, bytes)
        assert out == b"hello"

    def test_read_exactly_advances_position(self):
        r = _make_reader(b"abcdef")
        assert r.read_exactly(2) == b"ab"
        assert r.read_exactly(3) == b"cde"

    def test_read_until_returns_bytes(self):
        # Same bytearray-vs-bytes regression applies to ``read_until``.
        r = _make_reader(b"line1\r\nline2\r\n")
        out = r.read_until(b"\r\n")
        assert isinstance(out, bytes)
        assert out == b"line1\r\n"

    def test_read_until_multiple(self):
        r = _make_reader(b"a\r\nb\r\n")
        assert r.read_until(b"\r\n") == b"a\r\n"
        assert r.read_until(b"\r\n") == b"b\r\n"

    def test_read_exactly_raises_eof_on_short_data(self):
        r = _make_reader(b"abc")
        with pytest.raises(EOFError):
            r.read_exactly(10)

    def test_read_until_raises_eof_when_separator_missing(self):
        r = _make_reader(b"no_separator_here")
        with pytest.raises(EOFError):
            r.read_until(b"\r\n")

    def test_separator_across_chunk_boundary(self, monkeypatch):
        # The read buffer fills CHUNK_SIZE bytes at a time; ``read_until``
        # must handle a separator that straddles two chunks. Easiest way to
        # stress: drop CHUNK_SIZE to 4 so any 2-byte separator is likely to
        # straddle on real-world inputs.
        monkeypatch.setattr(FileStreamReader, "CHUNK_SIZE", 4)
        r = _make_reader(b"abc\r\ndef")
        assert r.read_until(b"\r\n") == b"abc\r\n"

    def test_at_eof_after_consuming_all_data(self):
        r = _make_reader(b"x")
        r.read_exactly(1)
        assert r._at_eof()

    def test_last_good_offset_starts_at_zero(self):
        # Truncate-to-offset-0 == "wipe the file"; the initial value must
        # be zero so a truncate before any successful parse correctly
        # discards everything (e.g., AOF starts with mid-command garbage).
        r = _make_reader(b"hello")
        assert r.get_last_good_offset() == 0

    def test_last_good_offset_tracks_buffer_consumption(self):
        r = _make_reader(b"abcdef")
        r.read_exactly(3)
        r.set_last_good_offset()
        assert r.get_last_good_offset() == 3
        r.read_exactly(2)
        r.set_last_good_offset()
        assert r.get_last_good_offset() == 5


# ========================== read_aof_command ========================


class TestReadAofCommand:
    """The sync RESP parser used during replay."""

    def test_parses_well_formed_command(self):
        r = _make_reader(b"*3\r\n$3\r\nSET\r\n$1\r\na\r\n$1\r\n1\r\n")
        assert read_aof_command(r) == [b"SET", b"a", b"1"]

    def test_command_args_are_bytes(self):
        # Argv values feed into ``COMMANDS.get(...)`` (via .upper()) and into
        # store handlers that ``hash()`` them as dict keys. bytearray here
        # would crash both.
        r = _make_reader(b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n")
        for arg in read_aof_command(r):
            assert isinstance(arg, bytes)

    def test_parses_multiple_commands_sequentially(self):
        r = _make_reader(
            b"*2\r\n$3\r\nGET\r\n$1\r\na\r\n"
            b"*2\r\n$3\r\nGET\r\n$1\r\nb\r\n"
        )
        assert read_aof_command(r) == [b"GET", b"a"]
        assert read_aof_command(r) == [b"GET", b"b"]

    def test_returns_none_at_clean_eof(self):
        # The "we read everything successfully" signal -- distinct from an
        # EOFError mid-frame (which indicates a torn write).
        r = _make_reader(b"")
        assert read_aof_command(r) is None

    def test_eof_mid_command_raises_eoferror(self):
        # Declared length 5 but only 3 bytes of body present -- classic
        # tail-truncation shape from a kill -9 between write and fsync.
        r = _make_reader(b"*2\r\n$3\r\nGET\r\n$5\r\nabc")
        with pytest.raises(EOFError):
            read_aof_command(r)

    def test_bad_array_prefix_raises_protocol_error(self):
        # A '+' (simple string) where the wire format requires '*' (array).
        # Mid-file corruption is fatal; the caller bubbles this out.
        r = _make_reader(b"+OK\r\n")
        with pytest.raises(ProtocolError):
            read_aof_command(r)

    def test_bad_bulk_string_prefix_raises_protocol_error(self):
        # Array header is fine, but the first element doesn't have a '$'.
        r = _make_reader(b"*1\r\n+OK\r\n")
        with pytest.raises(ProtocolError):
            read_aof_command(r)

    def test_last_good_offset_advances_after_successful_parse(self):
        # After parsing command N, the offset must point past command N so
        # a torn tail at the start of command N+1 truncates only N+1.
        first = b"*2\r\n$3\r\nGET\r\n$1\r\na\r\n"
        data = first + b"*2\r\n$3\r\nGET\r\n$1\r\nb\r\n"
        r = _make_reader(data)
        read_aof_command(r)
        assert r.get_last_good_offset() == len(first)

    def test_last_good_offset_unchanged_on_failed_parse(self):
        # Failure mid-second-command must leave the offset at the end of
        # the first -- truncating at that point preserves valid history.
        valid = b"*2\r\n$3\r\nGET\r\n$1\r\na\r\n"
        torn = b"*2\r\n$3\r\nGET\r\n$5\r\nab"  # length 5, body 2 bytes
        r = _make_reader(valid + torn)
        assert read_aof_command(r) == [b"GET", b"a"]
        with pytest.raises(EOFError):
            read_aof_command(r)
        assert r.get_last_good_offset() == len(valid)


# =========================== append_to_aof ==========================


class TestAppendToAof:
    def test_writes_to_main_when_no_snapshot_in_progress(self, aof_paths):
        main_path, temp_path = aof_paths
        # snapshot_pid is None at fixture entry (reset_store doesn't touch
        # it; the aof_paths fixture explicitly normalizes it in teardown).
        main = get_main_aof()
        main.open()
        try:
            append_to_aof(b"line\r\n")
        finally:
            main.close()
        assert main_path.read_bytes() == b"line\r\n"
        # Temp AOF was never opened -> file shouldn't even exist.
        assert not temp_path.exists()

    def test_writes_to_both_when_snapshot_in_progress(self, aof_paths):
        main_path, temp_path = aof_paths
        main = get_main_aof()
        temp = get_temp_aof()
        main.open()
        temp.open()
        # ``is_dump_in_progress()`` returns True iff ``snapshot_pid`` is
        # not None. Any truthy value works for the test.
        store_module.snapshot_pid = 99999
        try:
            append_to_aof(b"x\r\n")
        finally:
            store_module.snapshot_pid = None
            main.close()
            temp.close()
        assert main_path.read_bytes() == b"x\r\n"
        assert temp_path.read_bytes() == b"x\r\n"


# ========================== replay_dispatch =========================


class TestReplayDispatch:
    """The dispatcher used during AOF replay.

    Unlike live ``dispatch``, this one doesn't re-validate arity/type/etc
    (the commands already executed once on the live server, so they're
    known good) and threads transaction state through the caller's
    ``state`` dict.
    """

    async def test_single_command_applies(self):
        state = {"in_transaction": False, "queue": []}
        await replay_dispatch([b"SET", b"k", b"v"], state)
        assert store.get(b"k") == b"v"

    async def test_multi_exec_applies_inner_commands(self):
        # The whole point of the envelope: queue at MULTI, drain on EXEC.
        state = {"in_transaction": False, "queue": []}
        await replay_dispatch([b"MULTI"], state)
        await replay_dispatch([b"SET", b"a", b"1"], state)
        await replay_dispatch([b"SET", b"b", b"2"], state)
        await replay_dispatch([b"EXEC"], state)
        assert store.get(b"a") == b"1"
        assert store.get(b"b") == b"2"

    async def test_multi_queues_without_executing(self):
        # In-transaction commands must NOT be applied until EXEC -- this is
        # what makes torn-tail-before-EXEC safely discardable.
        state = {"in_transaction": False, "queue": []}
        await replay_dispatch([b"MULTI"], state)
        await replay_dispatch([b"SET", b"a", b"1"], state)
        assert store.get(b"a") is None
        assert state["in_transaction"] is True
        assert len(state["queue"]) == 1

    async def test_exec_resets_state(self):
        state = {"in_transaction": False, "queue": []}
        await replay_dispatch([b"MULTI"], state)
        await replay_dispatch([b"SET", b"a", b"1"], state)
        await replay_dispatch([b"EXEC"], state)
        assert state["in_transaction"] is False
        assert state["queue"] == []

    async def test_exec_without_multi_raises(self):
        # A stray EXEC means the AOF is structurally malformed; bubble up.
        state = {"in_transaction": False, "queue": []}
        with pytest.raises(Exception, match="EXEC"):
            await replay_dispatch([b"EXEC"], state)

    async def test_unknown_command_raises(self):
        # Commands removed in a downgrade, or sheer corruption that happens
        # to parse as RESP: must abort startup rather than silent-skip.
        state = {"in_transaction": False, "queue": []}
        with pytest.raises(Exception, match="Malformed AOF"):
            await replay_dispatch([b"NOSUCHCMD", b"arg"], state)


# ========================== replay_commands =========================


class TestReplayCommands:
    """End-to-end: pre-built AOF on disk -> replay_commands -> store state."""

    async def test_missing_aof_is_clean_skip(self, aof_paths):
        # First-ever startup -- no AOF, no error.
        main_path, _ = aof_paths
        assert not main_path.exists()
        await replay_commands()
        assert store._data == {}

    async def test_basic_set_replay(self, aof_paths):
        main_path, _ = aof_paths
        _write_aof(main_path, [b"SET", b"k", b"v"])
        await replay_commands()
        assert store.get(b"k") == b"v"

    async def test_replay_preserves_order(self, aof_paths):
        # Last-write-wins demands strict in-order replay. Swapping any two
        # SETs would give a different final value.
        main_path, _ = aof_paths
        _write_aof(main_path,
                   [b"SET", b"k", b"first"],
                   [b"SET", b"k", b"second"],
                   [b"SET", b"k", b"third"])
        await replay_commands()
        assert store.get(b"k") == b"third"

    async def test_all_data_types_round_trip(self, aof_paths):
        main_path, _ = aof_paths
        _write_aof(main_path,
                   [b"SET", b"s", b"hello"],
                   [b"RPUSH", b"l", b"x", b"y", b"z"],
                   [b"HSET", b"h", b"f1", b"v1"],
                   [b"ZADD", b"z", b"1", b"a", b"2", b"b"])
        await replay_commands()
        assert store.get(b"s") == b"hello"
        assert store.lrange(b"l", 0, -1) == [b"x", b"y", b"z"]
        assert store.hget(b"h", b"f1") == b"v1"
        assert store.zrange(b"z", 0, -1) == [b"a", b"b"]

    async def test_transaction_replay_applies_all_inner_commands(self, aof_paths):
        main_path, _ = aof_paths
        _write_aof(main_path,
                   [b"MULTI"],
                   [b"SET", b"a", b"1"],
                   [b"SET", b"b", b"2"],
                   [b"EXEC"])
        await replay_commands()
        assert store.get(b"a") == b"1"
        assert store.get(b"b") == b"2"

    async def test_transaction_atomicity_on_torn_tail(self, aof_paths):
        # AOF has MULTI + inner SETs but the EXEC is missing -- the process
        # crashed between writing the body and the EXEC marker. Replay
        # must discard the queued commands so atomicity-on-crash holds.
        main_path, _ = aof_paths
        _write_aof(main_path,
                   [b"SET", b"committed", b"yes"],
                   [b"MULTI"],
                   [b"SET", b"a", b"1"],
                   [b"SET", b"b", b"2"])
        await replay_commands()
        assert store.get(b"committed") == b"yes"
        # The transaction was never committed -- both writes must be gone.
        assert store.get(b"a") is None
        assert store.get(b"b") is None

    async def test_tail_truncation_repairs_file(self, aof_paths):
        # Three-step contract: (1) apply the valid prefix, (2) detect the
        # torn tail, (3) truncate the file at the last good offset so the
        # next ``open('ab')`` doesn't sit on top of garbage.
        main_path, _ = aof_paths
        valid = resp_encode_command([b"SET", b"k", b"v"])
        torn = b"*2\r\n$3\r\nGET\r\n$5\r\nab"  # length 5, body 2 bytes
        main_path.write_bytes(valid + torn)
        await replay_commands()
        assert store.get(b"k") == b"v"
        # File shrank back to the valid prefix.
        assert main_path.read_bytes() == valid

    async def test_mid_file_corruption_is_fatal(self, aof_paths):
        # Garbage in the MIDDLE (parseable bytes after the corruption point)
        # can't be safely truncated -- skipping forward would silently apply
        # a torso of state. The right answer is to refuse to start.
        main_path, _ = aof_paths
        valid = resp_encode_command([b"SET", b"k", b"v"])
        # ``:42\r\n`` parses to a complete RESP frame but its leading byte
        # isn't '*', so ``read_aof_command`` raises ProtocolError.
        main_path.write_bytes(valid + b":42\r\n" + valid)
        with pytest.raises(ProtocolError):
            await replay_commands()

    async def test_pexpireat_replay_preserves_ttl(self, aof_paths):
        # The AOF stores TTLs as absolute PEXPIREAT timestamps (ms) so that
        # replaying an hour later doesn't re-amortize the original TTL.
        # A future timestamp must round-trip into a positive TTL on reload.
        main_path, _ = aof_paths
        future_ms = int(time.time() * 1000) + 100_000  # 100s in the future
        _write_aof(main_path,
                   [b"SET", b"k", b"v"],
                   [b"PEXPIREAT", b"k", str(future_ms).encode()])
        await replay_commands()
        assert store.get(b"k") == b"v"
        # store.ttl returns -2 (missing), -1 (no ttl), or seconds remaining.
        assert store.ttl(b"k") > 0

    async def test_unknown_command_in_aof_aborts_startup(self, aof_paths):
        # An unknown command mid-AOF is fatal -- same rule as mid-file
        # corruption. We surface a Malformed-AOF error rather than skipping.
        main_path, _ = aof_paths
        _write_aof(main_path,
                   [b"SET", b"k", b"v"],
                   [b"NOSUCHCOMMAND", b"x"])
        with pytest.raises(Exception, match="Malformed AOF"):
            await replay_commands()


# ====================== end-to-end via dispatch =====================


class TestAofDispatchRoundTrip:
    """Drive the live ``dispatch`` (which appends), then ``replay_commands``.

    These are the strongest correctness tests in the file: they exercise the
    real encoders, the real append path, and the real replay path together.
    Anything that breaks the contract between live writes and replayed reads
    shows up here.
    """

    async def test_dispatch_then_replay_preserves_state(self, aof_paths):
        c = _dummy_client()
        main = get_main_aof()
        main.open()
        try:
            await _dispatch_and_append([b"SET", b"k", b"v"], c)
            await _dispatch_and_append([b"INCR", b"counter"], c)
            await _dispatch_and_append([b"INCR", b"counter"], c)
            await _dispatch_and_append([b"HSET", b"h", b"f", b"vv"], c)
        finally:
            main.close()

        # Wipe in-memory state and rebuild from the AOF.
        store._data.clear()
        store._ttl = RandomDict()
        await replay_commands()

        assert store.get(b"k") == b"v"
        assert store.get(b"counter") == b"2"
        assert store.hget(b"h", b"f") == b"vv"

    async def test_transaction_round_trips_through_aof(self, aof_paths):
        # MULTI/SET/EXEC don't take the per-command append path -- EXEC
        # builds its envelope and appends as a single buffered write.
        # So we drive ``dispatch`` directly here; the queued inner SETs
        # return QUEUED (no append_cmd anyway) and EXEC handles the write.
        c = _dummy_client()
        main = get_main_aof()
        main.open()
        try:
            await dispatch([b"MULTI"], c)
            await dispatch([b"SET", b"a", b"1"], c)
            await dispatch([b"SET", b"b", b"2"], c)
            await dispatch([b"EXEC"], c)
        finally:
            main.close()

        store._data.clear()
        store._ttl = RandomDict()
        await replay_commands()
        assert store.get(b"a") == b"1"
        assert store.get(b"b") == b"2"

    async def test_read_only_transaction_does_not_append(self, aof_paths):
        # EXEC only writes the MULTI/EXEC envelope when there was at least
        # one inner write (the ``had_writes`` guard). A transaction made up
        # entirely of reads should leave the AOF size unchanged.
        c = _dummy_client()
        main = get_main_aof()
        main.open()
        try:
            # Seed something so the AOF isn't empty (more realistic).
            await _dispatch_and_append([b"SET", b"k", b"v"], c)
            size_before = main.file.tell()

            await dispatch([b"MULTI"], c)
            await dispatch([b"GET", b"k"], c)
            await dispatch([b"EXISTS", b"k"], c)
            await dispatch([b"EXEC"], c)

            assert main.file.tell() == size_before
        finally:
            main.close()

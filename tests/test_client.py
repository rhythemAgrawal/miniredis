"""Unit tests for ClientState.

ClientState is the per-connection state container for transactions (and
pub/sub later). These tests cover its small state machine in isolation -- no
dispatch, no store, no asyncio.
"""
from miniredis.client import ClientState


class TestInitialState:
    def test_fresh_client_is_not_in_transaction(self):
        c = ClientState()
        assert c.in_transaction is False
        assert c.abort_transaction is False
        assert c.get_commands() == []


class TestStartAndClear:
    def test_start_transaction_enters_state(self):
        c = ClientState()
        c.start_transaction()
        assert c.in_transaction is True
        assert c.abort_transaction is False
        assert c.get_commands() == []

    def test_clear_transaction_leaves_state(self):
        c = ClientState()
        c.start_transaction()
        c.add_command([b"SET", b"k", b"v"])
        c.clear_transaction()
        assert c.in_transaction is False
        assert c.abort_transaction is False
        assert c.get_commands() == []

    def test_start_after_clear_starts_fresh(self):
        c = ClientState()
        c.start_transaction()
        c.add_command([b"SET", b"k", b"v"])
        c.mark_transaction_as_aborted()
        c.clear_transaction()
        # Second transaction must not inherit anything from the first.
        c.start_transaction()
        assert c.in_transaction is True
        assert c.abort_transaction is False
        assert c.get_commands() == []


class TestQueue:
    def test_commands_are_added_in_order(self):
        c = ClientState()
        c.start_transaction()
        c.add_command([b"SET", b"a", b"1"])
        c.add_command([b"INCR", b"a"])
        c.add_command([b"GET", b"a"])
        assert c.get_commands() == [
            [b"SET", b"a", b"1"],
            [b"INCR", b"a"],
            [b"GET", b"a"],
        ]

    def test_get_commands_returns_a_snapshot(self):
        # Modifying the returned list must not affect the internal queue,
        # because EXEC clears the queue and then iterates the snapshot.
        c = ClientState()
        c.start_transaction()
        c.add_command([b"SET", b"a", b"1"])
        commands = c.get_commands()
        commands.clear()
        # internal queue is still intact
        assert c.get_commands() == [[b"SET", b"a", b"1"]]


class TestAbortFlag:
    def test_mark_aborted_sets_flag(self):
        c = ClientState()
        c.start_transaction()
        c.mark_transaction_as_aborted()
        assert c.abort_transaction is True

    def test_clear_resets_abort_flag(self):
        c = ClientState()
        c.start_transaction()
        c.mark_transaction_as_aborted()
        c.clear_transaction()
        assert c.abort_transaction is False


class TestIsolation:
    def test_two_clients_have_independent_state(self):
        # Per-connection isolation: A's transaction must not leak into B.
        a = ClientState()
        b = ClientState()
        a.start_transaction()
        a.add_command([b"SET", b"x", b"1"])
        assert b.in_transaction is False
        assert b.get_commands() == []

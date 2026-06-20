"""End-to-end tests: the real redis-py client talking to a live server subprocess.

The ``miniredis_server`` fixture (in conftest.py) starts the server in its own
process and is function-scoped, so every test runs against a fresh, empty store.
These prove the whole stack -- RESP framing, dispatch, store -- interoperates
with an unmodified Redis client.
"""
import pytest
import redis


@pytest.fixture
def client(miniredis_server):
    host, port = miniredis_server
    conn = redis.Redis(host=host, port=port)
    yield conn
    conn.close()


def test_set_and_get(client):
    client.set("foo", "bar")
    assert client.get("foo") == b"bar"


def test_get_missing_returns_none(client):
    assert client.get("nope") is None


def test_incr_and_decr(client):
    assert client.incr("counter") == 1
    assert client.incr("counter") == 2
    assert client.decr("counter") == 1


def test_incrby_and_decrby(client):
    assert client.incrby("c", 10) == 10
    assert client.decrby("c", 4) == 6


def test_append_and_strlen(client):
    assert client.append("k", "ab") == 2
    assert client.append("k", "cd") == 4
    assert client.strlen("k") == 4
    assert client.get("k") == b"abcd"


def test_del_and_exists(client):
    client.set("a", "1")
    client.set("b", "2")
    assert client.exists("a", "b") == 2
    assert client.delete("a") == 1
    assert client.exists("a") == 0


def test_expire_and_ttl(client):
    client.set("k", "v")
    assert client.expire("k", 100) is True
    assert 90 <= client.ttl("k") <= 100


def test_set_with_ex_option(client):
    client.set("k", "v", ex=100)
    assert 90 <= client.ttl("k") <= 100


def test_persist(client):
    client.set("k", "v", ex=100)
    assert client.persist("k") is True
    assert client.ttl("k") == -1


def test_incr_preserves_ttl_end_to_end(client):
    client.set("c", "5", ex=100)
    assert client.incr("c") == 6
    assert 90 <= client.ttl("c") <= 100


def test_pipelined_commands(client):
    # Multiple commands on one connection, read back in order -- exercises the
    # server's per-connection command loop.
    pipe = client.pipeline(transaction=False)
    pipe.set("a", "1")
    pipe.incr("a")
    pipe.get("a")
    assert pipe.execute() == [True, 2, b"2"]


def test_list_push_range_pop(client):
    assert client.rpush("l", "a", "b", "c") == 3
    assert client.lrange("l", 0, -1) == [b"a", b"b", b"c"]
    assert client.lpush("l", "z") == 4
    assert client.lrange("l", 0, 0) == [b"z"]
    assert client.lpop("l") == b"z"
    assert client.rpop("l") == b"c"
    assert client.llen("l") == 2


def test_lpop_with_count(client):
    client.rpush("l", "a", "b", "c")
    assert client.lpop("l", 2) == [b"a", b"b"]


def test_lpop_on_missing_key_returns_none(client):
    assert client.lpop("missing") is None


def test_hash_roundtrip(client):
    assert client.hset("h", mapping={"a": "1", "b": "2"}) == 2
    assert client.hget("h", "a") == b"1"
    assert client.hgetall("h") == {b"a": b"1", b"b": b"2"}
    assert client.hlen("h") == 2
    assert sorted(client.hkeys("h")) == [b"a", b"b"]
    assert sorted(client.hvals("h")) == [b"1", b"2"]
    assert client.hdel("h", "a") == 1
    assert client.hget("h", "a") is None


def test_wrongtype_error_both_directions(client):
    client.set("s", "v")
    with pytest.raises(redis.exceptions.ResponseError, match="WRONGTYPE"):
        client.lpush("s", "x")

    client.rpush("l", "a")
    with pytest.raises(redis.exceptions.ResponseError, match="WRONGTYPE"):
        client.get("l")


def test_sorted_set_commands(client):
    assert client.zadd("z", {"a": 1, "b": 2, "c": 3}) == 3
    assert client.zscore("z", "a") == 1.0
    assert client.zrange("z", 0, -1) == [b"a", b"b", b"c"]
    assert client.zrank("z", "b") == 1
    assert client.zrangebyscore("z", 2, 3) == [b"b", b"c"]
    assert client.zrem("z", "a") == 1
    assert client.zrange("z", 0, -1) == [b"b", b"c"]


def test_zadd_update_repositions(client):
    client.zadd("z", {"a": 1, "b": 2})
    client.zadd("z", {"a": 5})
    assert client.zrange("z", 0, -1) == [b"b", b"a"]


def test_zrank_missing_member_is_none(client):
    client.zadd("z", {"a": 1})
    assert client.zrank("z", "x") is None


def test_sorted_set_wrongtype(client):
    client.set("s", "v")
    with pytest.raises(redis.exceptions.ResponseError, match="WRONGTYPE"):
        client.zadd("s", {"a": 1})


def test_sorted_set_infinity_scores(client):
    client.zadd("z", {"a": float("inf"), "b": float("-inf"), "c": 5})
    assert client.zscore("z", "a") == float("inf")
    assert client.zscore("z", "b") == float("-inf")
    assert client.zrange("z", 0, -1) == [b"b", b"c", b"a"]
    assert client.zrangebyscore("z", float("-inf"), float("inf")) == [b"b", b"c", b"a"]
    assert client.zrangebyscore("z", float("-inf"), 5) == [b"b", b"c"]


# -- Transactions (MULTI / EXEC / DISCARD) over the wire --------------------


def test_transaction_basic_pipeline(client):
    """redis-py's pipeline(transaction=True) issues MULTI/EXEC."""
    pipe = client.pipeline(transaction=True)
    pipe.set("a", "1")
    pipe.set("b", "2")
    pipe.get("a")
    pipe.get("b")
    assert pipe.execute() == [True, True, b"1", b"2"]
    # And the writes are visible to other commands afterward.
    assert client.get("a") == b"1"
    assert client.get("b") == b"2"


def test_transaction_atomic_increment(client):
    """The canonical example: increment, read, both in one batch."""
    client.set("counter", "10")
    pipe = client.pipeline(transaction=True)
    pipe.incr("counter")
    pipe.incr("counter")
    pipe.get("counter")
    assert pipe.execute() == [11, 12, b"12"]


def test_transaction_discard(client):
    """pipe.reset() discards the queued commands without running them."""
    pipe = client.pipeline(transaction=True)
    pipe.set("a", "queued_only")
    pipe.reset()
    # The SET was never applied.
    assert client.get("a") is None


def test_transaction_runtime_error_in_batch_does_not_abort_others(client):
    """A WRONGTYPE inside the batch goes into the result array; the other
    queued commands still execute."""
    client.rpush("l", "x")   # l is now a list
    pipe = client.pipeline(transaction=True)
    pipe.get("l")            # WRONGTYPE at exec time
    pipe.set("ok", "yes")    # still runs
    results = pipe.execute(raise_on_error=False)
    # First element: the WRONGTYPE error
    assert isinstance(results[0], redis.exceptions.ResponseError)
    assert "WRONGTYPE" in str(results[0])
    # Second element: the SET went through
    assert results[1] is True
    assert client.get("ok") == b"yes"


def test_transaction_queue_time_arity_error_aborts(client):
    """A queue-time arity error must abort the whole batch with EXECABORT."""
    # We can't easily synthesize a malformed command through redis-py's
    # type-checked API, so drop to the raw connection and speak RESP directly.
    raw = client.connection_pool.get_connection()
    try:
        raw.send_command("MULTI")
        assert raw.read_response() == b"OK"

        # Send a valid SET, get +QUEUED.
        raw.send_command("SET", "k", "v")
        assert raw.read_response() == b"QUEUED"

        # Send GET with no args -- arity error, txn aborted.
        raw.send_command("GET")
        with pytest.raises(redis.exceptions.ResponseError):
            raw.read_response()

        # EXEC must return EXECABORT. (redis-py strips the prefix when raising,
        # so we match against the rest of the message.)
        raw.send_command("EXEC")
        with pytest.raises(redis.exceptions.ResponseError, match="discarded"):
            raw.read_response()

        # After EXECABORT the connection is cleanly out of the transaction,
        # so we can keep using it. The queued SET was rejected with the batch.
        raw.send_command("GET", "k")
        assert raw.read_response() is None
    finally:
        client.connection_pool.release(raw)


def test_multiple_clients_have_independent_transactions(miniredis_server):
    """Two TCP connections each have their own MULTI state."""
    host, port = miniredis_server
    a = redis.Redis(host=host, port=port)
    b = redis.Redis(host=host, port=port)
    try:
        pipe_a = a.pipeline(transaction=True)
        pipe_a.set("shared", "from_a")

        # While A's pipeline is queued, B can write through immediately.
        assert b.set("shared", "from_b") is True
        assert b.get("shared") == b"from_b"

        # Now A's EXEC overwrites.
        assert pipe_a.execute() == [True]
        assert b.get("shared") == b"from_a"
    finally:
        a.close()
        b.close()


# -- Pub/sub (SUBSCRIBE / PSUBSCRIBE / PUBLISH / UNSUBSCRIBE) over the wire ----


def _drain_subscribe_acks(pubsub, n):
    """Helper: read and discard the next N subscribe/psubscribe ack frames."""
    for _ in range(n):
        m = pubsub.get_message(timeout=1)
        assert m is not None and m["type"] in {"subscribe", "psubscribe",
                                                "unsubscribe", "punsubscribe"}


def test_pubsub_basic_subscribe_and_publish(miniredis_server):
    host, port = miniredis_server
    sub = redis.Redis(host=host, port=port)
    pub = redis.Redis(host=host, port=port)
    try:
        ps = sub.pubsub()
        ps.subscribe("ch")
        _drain_subscribe_acks(ps, 1)

        # Publisher reports 1 receiver.
        assert pub.publish("ch", "hello") == 1

        msg = ps.get_message(timeout=1)
        assert msg is not None
        assert msg["type"] == "message"
        assert msg["channel"] == b"ch"
        assert msg["data"] == b"hello"
    finally:
        ps.close()
        sub.close()
        pub.close()


def test_pubsub_publish_with_no_subscribers_returns_zero(client):
    assert client.publish("nobody-here", "x") == 0


def test_pubsub_multiple_subscribers_each_get_the_message(miniredis_server):
    host, port = miniredis_server
    a = redis.Redis(host=host, port=port)
    b = redis.Redis(host=host, port=port)
    pub = redis.Redis(host=host, port=port)
    try:
        psa = a.pubsub(); psb = b.pubsub()
        psa.subscribe("ch"); psb.subscribe("ch")
        _drain_subscribe_acks(psa, 1)
        _drain_subscribe_acks(psb, 1)

        assert pub.publish("ch", "broadcast") == 2

        for ps in (psa, psb):
            m = ps.get_message(timeout=1)
            assert m["type"] == "message"
            assert m["data"] == b"broadcast"
    finally:
        psa.close(); psb.close()
        a.close(); b.close(); pub.close()


def test_pubsub_pattern_subscribe_receives_matching_channels(miniredis_server):
    host, port = miniredis_server
    sub = redis.Redis(host=host, port=port)
    pub = redis.Redis(host=host, port=port)
    try:
        ps = sub.pubsub()
        ps.psubscribe("news.*")
        _drain_subscribe_acks(ps, 1)

        assert pub.publish("news.tech", "story") == 1

        m = ps.get_message(timeout=1)
        assert m["type"] == "pmessage"
        assert m["pattern"] == b"news.*"
        assert m["channel"] == b"news.tech"
        assert m["data"] == b"story"

        # A channel that doesn't match the pattern: no message arrives.
        pub.publish("alerts.urgent", "x")
        assert ps.get_message(timeout=0.2) is None
    finally:
        ps.close()
        sub.close()
        pub.close()


def test_pubsub_unsubscribe_stops_delivery(miniredis_server):
    host, port = miniredis_server
    sub = redis.Redis(host=host, port=port)
    pub = redis.Redis(host=host, port=port)
    try:
        ps = sub.pubsub()
        ps.subscribe("ch")
        _drain_subscribe_acks(ps, 1)

        assert pub.publish("ch", "first") == 1
        m1 = ps.get_message(timeout=1)
        assert m1["data"] == b"first"

        ps.unsubscribe("ch")
        _drain_subscribe_acks(ps, 1)   # the unsubscribe ack itself

        # After unsubscribe, no subscribers -> publisher reports 0.
        assert pub.publish("ch", "second") == 0
        assert ps.get_message(timeout=0.2) is None
    finally:
        ps.close()
        sub.close()
        pub.close()


def test_pubsub_non_pubsub_command_in_subscribe_mode_is_rejected(miniredis_server):
    # While subscribed, you can't issue arbitrary commands on the same connection.
    host, port = miniredis_server
    sub = redis.Redis(host=host, port=port)
    try:
        ps = sub.pubsub()
        ps.subscribe("ch")
        _drain_subscribe_acks(ps, 1)
        # Send a GET through the raw connection of the pubsub object.
        ps.connection.send_command("GET", "k")
        with pytest.raises(redis.exceptions.ResponseError, match="in this context"):
            ps.connection.read_response()
    finally:
        ps.close()
        sub.close()


def test_pubsub_ping_works_while_subscribed(miniredis_server):
    host, port = miniredis_server
    sub = redis.Redis(host=host, port=port)
    try:
        ps = sub.pubsub()
        ps.subscribe("ch")
        _drain_subscribe_acks(ps, 1)
        # PING is whitelisted in subscribe mode; should not error.
        ps.connection.send_command("PING")
        assert ps.connection.read_response() == b"PONG"
    finally:
        ps.close()
        sub.close()


def test_pubsub_subscribe_rejected_inside_multi(client):
    # We disallow SUBSCRIBE (and family) inside MULTI: the queue-time error
    # marks the transaction aborted and EXEC returns EXECABORT.
    raw = client.connection_pool.get_connection()
    try:
        raw.send_command("MULTI")
        assert raw.read_response() == b"OK"
        raw.send_command("SUBSCRIBE", "x")
        with pytest.raises(redis.exceptions.ResponseError,
                            match="not allowed in transactions"):
            raw.read_response()
        raw.send_command("EXEC")
        with pytest.raises(redis.exceptions.ResponseError, match="discarded"):
            raw.read_response()
    finally:
        client.connection_pool.release(raw)

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

import redis

def test_set_get(miniredis_server):
    host, port = miniredis_server
    r = redis.Redis(host=host, port=port)
    r.set("foo", "bar")
    assert r.get("foo") == b"bar"

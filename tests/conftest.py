# tests/conftest.py
import socket
import subprocess
import time
import pytest
import sys


@pytest.fixture
def miniredis_server():
    # Start your server as a separate process
    proc = subprocess.Popen([sys.executable, "-m", "miniredis"])

    # Wait until it's actually accepting connections
    _wait_for_port("127.0.0.1", 6380, timeout=5.0)

    yield "127.0.0.1", 6380          # value injected into the test

    # Teardown runs after the test, even if it failed
    proc.terminate()
    proc.wait(timeout=5)


def _wait_for_port(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"miniredis didn't start on {host}:{port}")

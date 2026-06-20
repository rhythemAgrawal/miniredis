"""Tiny smoke tests for the pydantic-settings Config.

The defaults below are part of the operational contract -- changing them
silently can subtly break deployments. If you intentionally update a default,
update the assertion here in the same commit.
"""
import pytest

from miniredis.config import Settings, get_settings


class TestSettingsDefaults:
    def test_default_snapshot_path(self):
        s = Settings()
        assert s.snapshot_path == "dump.rdb"

    def test_default_max_save_timeout(self):
        s = Settings()
        assert s.max_save_timeout == 3600

    def test_default_buffer_drain_timeout(self):
        s = Settings()
        # New for Day 6 -- bounds the handle_request shutdown drain wait.
        assert hasattr(s, "buffer_drain_timeout"), \
            "buffer_drain_timeout must exist for the pubsub drain path"


class TestSettingsEnvOverride:
    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("SNAPSHOT_PATH", "/tmp/custom.rdb")
        # Construct a fresh Settings (the cached get_settings() is module-level
        # and won't pick up env changes mid-process).
        s = Settings()
        assert s.snapshot_path == "/tmp/custom.rdb"

    def test_case_insensitive_env_vars(self, monkeypatch):
        # config.py sets `case_sensitive=False`, so lowercase env vars work too.
        monkeypatch.setenv("max_save_timeout", "1800")
        s = Settings()
        assert s.max_save_timeout == 1800


class TestGetSettingsCache:
    def test_get_settings_returns_same_instance(self):
        # @cache decoration means callers share one Settings instance;
        # monkeypatching attributes on it (as test_persistence does) propagates.
        assert get_settings() is get_settings()

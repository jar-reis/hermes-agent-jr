"""Tests for loopback-only API_SERVER_KEY auto-provisioning.

Covers ``ensure_api_server_key()`` — the gateway-startup preflight that
self-heals an enabled-but-unkeyed API server on a loopback bind (bd
hermes-2gjd) while deliberately leaving network binds to ``connect()``'s
hard refuse-with-guidance.

The contract these tests pin:

- loopback + no key  -> generate, persist to ~/.hermes/.env, mirror into config
- key already set    -> no-op (env OR config.extra['key'])
- network bind       -> no-op, do NOT mint a key for a public surface
- the low-level connect() guard stays intact as defense-in-depth
"""

import os
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    ensure_api_server_key,
)


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a throwaway dir so save_env_value() can't touch
    the developer's real ~/.hermes/.env."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # get_env_path()/load_env() memoise on the .env mtime; clear the memo so a
    # fresh home is seen even if an earlier test in the process warmed it.
    from hermes_cli import config as hermes_config

    hermes_config.invalidate_env_cache()
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    return home


class TestEnsureApiServerKey:
    def test_loopback_without_key_provisions_and_persists(self, isolated_hermes_home):
        config = PlatformConfig(enabled=True, extra={"host": "127.0.0.1"})

        key = ensure_api_server_key(config)

        # A strong 256-bit hex key (secrets.token_hex(32) -> 64 hex chars).
        assert key is not None
        assert len(key) == 64
        int(key, 16)  # valid hex — raises if not

        # Mirrored into the config the adapter is about to read.
        assert config.extra["key"] == key
        # Mirrored into the process env.
        assert os.environ["API_SERVER_KEY"] == key
        # Persisted to the gitignored ~/.hermes/.env for restart survival.
        env_file = isolated_hermes_home / ".env"
        assert env_file.exists()
        assert f"API_SERVER_KEY={key}" in env_file.read_text()

    def test_default_host_is_loopback_and_provisions(self, isolated_hermes_home):
        # No host in extra and no API_SERVER_HOST env -> DEFAULT_HOST 127.0.0.1.
        config = PlatformConfig(enabled=True, extra={})
        key = ensure_api_server_key(config)
        assert key is not None
        assert config.extra["key"] == key

    def test_existing_config_key_is_noop(self, isolated_hermes_home):
        config = PlatformConfig(
            enabled=True, extra={"host": "127.0.0.1", "key": "sk-preset"}
        )
        assert ensure_api_server_key(config) is None
        assert config.extra["key"] == "sk-preset"
        # Nothing written to .env.
        assert not (isolated_hermes_home / ".env").exists()

    def test_existing_env_key_is_noop(self, isolated_hermes_home, monkeypatch):
        monkeypatch.setenv("API_SERVER_KEY", "sk-from-env")
        config = PlatformConfig(enabled=True, extra={"host": "127.0.0.1"})
        assert ensure_api_server_key(config) is None
        assert "key" not in config.extra
        assert not (isolated_hermes_home / ".env").exists()

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.1", "192.168.1.5"])
    def test_network_bind_never_provisions(self, isolated_hermes_home, host):
        config = PlatformConfig(enabled=True, extra={"host": host})
        assert ensure_api_server_key(config) is None
        assert "key" not in config.extra
        assert not (isolated_hermes_home / ".env").exists()

    def test_provisioned_key_satisfies_adapter(self, isolated_hermes_home):
        """After provisioning, the adapter built from the same config carries
        the key — so the connect() guard would no longer refuse."""
        config = PlatformConfig(enabled=True, extra={"host": "127.0.0.1"})
        key = ensure_api_server_key(config)
        adapter = APIServerAdapter(config)
        assert adapter._api_key == key
        assert adapter._api_key != ""

    def test_failure_is_swallowed(self, isolated_hermes_home, monkeypatch):
        """A save_env_value() failure must not raise — provisioning fails open
        and the adapter's own guard refuses cleanly instead."""
        import hermes_cli.config as hermes_config

        def _boom(*_a, **_k):
            raise OSError("read-only home")

        monkeypatch.setattr(hermes_config, "save_env_value", _boom)
        config = PlatformConfig(enabled=True, extra={"host": "127.0.0.1"})
        # Does not raise; returns None because the key never landed.
        assert ensure_api_server_key(config) is None

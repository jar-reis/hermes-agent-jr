import json
from datetime import datetime


def test_compressor_publishes_atomic_context_snapshot(tmp_path):
    from agent.context_compressor import ContextCompressor

    snapshot = tmp_path / "cron-session.json"
    compressor = ContextCompressor("test-model", quiet_mode=True, config_context_length=100_000)
    compressor.configure_context_snapshot(snapshot, "cron-session")
    compressor.update_from_response({"prompt_tokens": 1234, "completion_tokens": 5})

    payload = json.loads(snapshot.read_text())
    assert payload["last_prompt_tokens"] == 1234
    assert payload["context_length"] == compressor.context_length
    assert payload["source"] == "context_compressor"
    assert payload["session_id"] == "cron-session"
    assert datetime.fromisoformat(payload["timestamp"]).tzinfo is not None
    assert not list(tmp_path.glob("*.tmp"))


def test_context_snapshot_vars_bridge_to_local_subprocess_env(tmp_path):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.environments.local import _make_run_env

    snapshot = tmp_path / "snapshot.json"
    tokens = set_session_vars(session_id="cron-abc", context_snapshot=str(snapshot))
    try:
        env = _make_run_env({})
        assert env["HERMES_SESSION_ID"] == "cron-abc"
        assert env["HERMES_CONTEXT_SNAPSHOT"] == str(snapshot)
    finally:
        clear_session_vars(tokens)


def test_cleared_snapshot_context_suppresses_stale_process_env(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.environments.local import _make_run_env

    monkeypatch.setenv("HERMES_CONTEXT_SNAPSHOT", "/tmp/stale-other-session.json")
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-other-session")
    tokens = set_session_vars(session_id="", context_snapshot="")
    try:
        env = _make_run_env({})
        assert "HERMES_CONTEXT_SNAPSHOT" not in env
        assert "HERMES_SESSION_ID" not in env
    finally:
        clear_session_vars(tokens)

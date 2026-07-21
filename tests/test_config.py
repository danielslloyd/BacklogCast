"""Config accessors: env overrides win over config.json defaults."""
from app import config


def test_lloydio_poll_seconds_env_wins(monkeypatch):
    monkeypatch.setenv("LLOYDIO_POLL_SECONDS", "900")
    assert config.lloydio_poll_seconds() == 900
    monkeypatch.setenv("LLOYDIO_POLL_SECONDS", "notanint")
    assert config.lloydio_poll_seconds() == 0  # bad env -> config default (0)
    monkeypatch.delenv("LLOYDIO_POLL_SECONDS", raising=False)
    assert config.lloydio_poll_seconds() == 0


def test_env_overrides_for_henty_and_asr(monkeypatch):
    monkeypatch.setenv("HENTY_BASE_URL", "http://gpu.local:5000/")
    monkeypatch.setenv("ASR_SIMILARITY_THRESHOLD", "0.7")
    assert config.henty_base_url() == "http://gpu.local:5000"  # trailing slash stripped
    assert config.asr_threshold() == 0.7

"""Read/write global config and tokens."""
from __future__ import annotations

import json
import os
import secrets
from typing import Any

from .paths import CONFIG_PATH, DEFAULT_CONFIG, TOKENS_PATH


def load_config() -> dict[str, Any]:
    data = json.loads(CONFIG_PATH.read_text())
    # merge in any new default keys
    merged = {**DEFAULT_CONFIG, **data}
    return merged


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    current = load_config()
    # only let known keys through
    for k in DEFAULT_CONFIG:
        if k in cfg:
            current[k] = cfg[k]
    CONFIG_PATH.write_text(json.dumps(current, indent=2))
    return current


def public_base_url(request_host: str | None = None) -> str:
    cfg = load_config()
    env = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if env:
        return env.rstrip("/")
    configured = cfg.get("public_base_url", "").strip()
    if configured:
        return configured.rstrip("/")
    if request_host:
        return request_host.rstrip("/")
    return "http://127.0.0.1:8000"


# --- integration settings (env var wins over config.json) -------------------

def _setting(env_key: str, cfg_key: str, default: str = "") -> str:
    env = os.environ.get(env_key, "").strip()
    if env:
        return env
    return str(load_config().get(cfg_key, default)).strip()


def lloydio_base_url() -> str:
    return _setting("LLOYDIO_BASE_URL", "lloydio_base_url").rstrip("/")


def henty_base_url() -> str:
    return _setting("HENTY_BASE_URL", "henty_base_url", "http://127.0.0.1:5000").rstrip("/")


def henty_api_key() -> str:
    # Secret: env-only, never persisted to config.json.
    return os.environ.get("HENTY_API_KEY", "").strip()


def henty_books_dir() -> str:
    return _setting("HENTY_BOOKS_DIR", "henty_books_dir")


def default_voice() -> str:
    return _setting("DEFAULT_VOICE", "default_voice", "Haggard")


def asr_threshold() -> float:
    env = os.environ.get("ASR_SIMILARITY_THRESHOLD", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        return float(load_config().get("asr_similarity_threshold", 0.85))
    except (TypeError, ValueError):
        return 0.85


def asr_max_retries() -> int:
    env = os.environ.get("ASR_MAX_RETRIES", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        return int(load_config().get("asr_max_retries", 4))
    except (TypeError, ValueError):
        return 4


def lloydio_poll_seconds() -> int:
    env = os.environ.get("LLOYDIO_POLL_SECONDS", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        return int(load_config().get("lloydio_poll_seconds", 0))
    except (TypeError, ValueError):
        return 0


def load_tokens() -> dict[str, str]:
    return json.loads(TOKENS_PATH.read_text())


def save_tokens(tokens: dict[str, str]) -> None:
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))


def create_token(name: str) -> str:
    tokens = load_tokens()
    token = secrets.token_urlsafe(32)
    tokens[name] = token
    save_tokens(tokens)
    return token


def revoke_token(token: str) -> bool:
    tokens = load_tokens()
    for name, value in list(tokens.items()):
        if value == token:
            del tokens[name]
            save_tokens(tokens)
            return True
    return False


def token_valid(token: str) -> bool:
    return token in load_tokens().values()

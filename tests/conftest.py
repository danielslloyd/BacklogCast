"""Pytest setup: isolate BACKLOGCAST_DATA to a temp dir before app import."""
import os
import shutil
import tempfile

# Must be set before any `app.paths` import (module-level ROOT reads it once).
os.environ.setdefault("BACKLOGCAST_DATA", tempfile.mkdtemp(prefix="blc-test-"))
# Never shell out to the real Claude CLI from tests (tests opt in by mocking it).
os.environ["CLAUDE_EXTRACT"] = "off"
# Never write back to a real lloydio from tests.
os.environ.pop("LLOYDIO_EPISODE_TOKEN", None)

import pytest  # noqa: E402

from app import paths  # noqa: E402


@pytest.fixture(autouse=True)
def clean_data():
    """Give each test a fresh data tree (config, tokens, and articles)."""
    for p in (paths.CONFIG_PATH, paths.TOKENS_PATH):
        if p.exists():
            p.unlink()
    if paths.ARTICLES_DIR.exists():
        shutil.rmtree(paths.ARTICLES_DIR)
    paths.ensure_data_tree()
    yield

"""Pytest setup: isolate BACKLOGCAST_DATA to a temp dir before app import."""
import os
import shutil
import tempfile

# Must be set before any `app.paths` import (module-level ROOT reads it once).
os.environ.setdefault("BACKLOGCAST_DATA", tempfile.mkdtemp(prefix="blc-test-"))

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

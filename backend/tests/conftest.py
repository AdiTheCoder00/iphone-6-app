"""Shared fixtures for the backend test suite.

Every test runs against its own throwaway SQLite file: the store is a module
singleton, so the autouse fixture re-points its db_path before anything calls
store.init(). The event hub is also a singleton — each test wipes any
subscribers an earlier test left behind.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    return settings


@pytest.fixture
def fresh_store(isolated_settings):
    from app.services.store import store

    store.init()
    yield store
    store.close()


@pytest.fixture
def no_subscribers():
    """Guarantee subscriber_count == 0 (precondition of the "held" path)."""
    from app.services.events import event_hub

    for queue in list(event_hub._subscribers):
        event_hub.unsubscribe(queue)
    yield
    for queue in list(event_hub._subscribers):
        event_hub.unsubscribe(queue)
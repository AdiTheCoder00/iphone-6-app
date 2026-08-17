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


@pytest.fixture(autouse=True)
def no_background_preloads(monkeypatch):
    """The lifespan eagerly preloads Whisper and TTS — network calls and GBs
    of model downloads. Unit tests must never do that, so stub the preloads
    out for the whole suite. The lazy first-use paths they stand in for are
    out of scope here and load on first call in production anyway."""
    from app.services import tts as tts_module
    from app.services.transcription import transcription_service

    async def _noop() -> None:
        return None

    monkeypatch.setattr(tts_module, "preload", _noop)
    monkeypatch.setattr(transcription_service, "preload", _noop)


@pytest.fixture(autouse=True)
def fresh_store(isolated_settings):
    """Every test runs against its own throwaway SQLite file, whatever order
    the suite executes in — a test that reaches the store must never inherit
    (or leak) the previous test's connection."""
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
"""SQLite persistence for reminders and conversation history.

Both used to live in memory, which meant a `uvicorn` restart silently dropped
every reminder the user had set — the worst possible failure for the one
feature whose entire value is remembering something. Follows guru-rag-app's
history.db pattern: one file, plain sqlite3, no ORM.

Times are stored as epoch floats rather than ISO strings so "what is due" is a
numeric comparison the database can index.
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL,
    fire_time   REAL    NOT NULL,
    created_at  REAL    NOT NULL,
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (fired, fire_time);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages (created_at);

-- Durable things the companion knows about the user, as opposed to `messages`
-- which is only the recent transcript. These are injected into the system
-- prompt on every turn, so the table is deliberately small and the text short.
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL UNIQUE,
    created_at  REAL    NOT NULL
);
"""


class Store:
    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        # sqlite3 connections are not safe to share across threads without
        # serialising; every call here runs inside this lock.
        self._lock = threading.Lock()

    def init(self) -> None:
        path = Path(settings.resolved_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL so the poller reading due reminders never blocks a write
            # from the chat path.
            self._conn.execute("PRAGMA journal_mode=WAL")
            # A second process (a stray uvicorn, a DB inspector) would
            # otherwise raise "database is locked" instead of waiting.
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        logger.info("Store ready at %s", path)

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
            self._conn = None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store.init() has not been called")
        return self._conn

    # --- reminders ------------------------------------------------------

    def add_reminder(self, text: str, fire_time: float) -> dict:
        conn = self._require()
        now = time.time()
        with self._lock:
            cur = conn.execute(
                "INSERT INTO reminders (text, fire_time, created_at) VALUES (?, ?, ?)",
                (text, fire_time, now),
            )
            conn.commit()
        return {"id": cur.lastrowid, "text": text, "fire_time": fire_time, "created_at": now}

    def due_reminders(self, now: float | None = None) -> list[dict]:
        conn = self._require()
        cutoff = time.time() if now is None else now
        with self._lock:
            rows = conn.execute(
                "SELECT id, text, fire_time FROM reminders WHERE fired = 0 AND fire_time <= ?"
                " ORDER BY fire_time",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_fired(self, reminder_id: int) -> bool:
        """Returns False if another worker already claimed this one.

        The UPDATE is guarded on fired = 0 so the claim is atomic: two pollers
        cannot both fire the same reminder.
        """
        conn = self._require()
        with self._lock:
            cur = conn.execute(
                "UPDATE reminders SET fired = 1 WHERE id = ? AND fired = 0", (reminder_id,)
            )
            conn.commit()
        return cur.rowcount > 0

    def snooze_fired_reminder(self, reminder_id: int, fire_time: float) -> dict | None:
        """Reactivate a delivered reminder at a later time.

        The fired predicate makes a snooze a one-time action: two devices
        receiving the same SSE event cannot both move the reminder.
        """
        conn = self._require()
        with self._lock:
            row = conn.execute(
                "SELECT id, text FROM reminders WHERE id = ? AND fired = 1", (reminder_id,)
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE reminders SET fire_time = ?, fired = 0 WHERE id = ? AND fired = 1",
                (fire_time, reminder_id),
            )
            conn.commit()
        if cur.rowcount == 0:
            return None
        return {"id": row["id"], "text": row["text"], "fire_time": fire_time}

    def pending_reminders(self) -> list[dict]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT id, text, fire_time FROM reminders WHERE fired = 0 ORDER BY fire_time"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_reminder(self, reminder_id: int) -> bool:
        """Remove a pending reminder outright.

        Deleted rather than marked fired: a cancelled reminder is not one that
        happened, and leaving it as fired would misreport it in any later view.
        """
        conn = self._require()
        with self._lock:
            cur = conn.execute(
                "DELETE FROM reminders WHERE id = ? AND fired = 0", (reminder_id,)
            )
            conn.commit()
        return cur.rowcount > 0

    # --- facts ----------------------------------------------------------

    def add_fact(self, text: str) -> dict | None:
        """Store a durable fact. Returns None if it is already known."""
        conn = self._require()
        now = time.time()
        with self._lock:
            try:
                cur = conn.execute(
                    "INSERT INTO facts (text, created_at) VALUES (?, ?)", (text, now)
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # UNIQUE violation: the model re-remembered something.
                return None
        return {"id": cur.lastrowid, "text": text, "created_at": now}

    def list_facts(self, limit: int = 100) -> list[dict]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT id, text FROM facts ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def fact_count(self) -> int:
        conn = self._require()
        with self._lock:
            return conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def delete_fact(self, fact_id: int) -> bool:
        conn = self._require()
        with self._lock:
            cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            conn.commit()
        return cur.rowcount > 0

    def clear_facts(self) -> int:
        conn = self._require()
        with self._lock:
            cur = conn.execute("DELETE FROM facts")
            conn.commit()
        return cur.rowcount

    # --- conversation ---------------------------------------------------

    def append_message(self, role: str, content: str) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
                (role, content, time.time()),
            )
            # A recent transcript gives the companion continuity, but retaining
            # every turn forever makes a desk device's database grow without a
            # bound.  Honour a larger replay setting if the user configured one.
            keep = max(settings.conversation_store_limit, settings.history_replay_limit)
            conn.execute(
                "DELETE FROM messages WHERE id NOT IN ("
                "SELECT id FROM messages ORDER BY id DESC LIMIT ?"
                ")",
                (keep,),
            )
            conn.commit()

    def recent_messages(self, limit: int = 20) -> list[dict]:
        """Most recent `limit` messages, returned oldest-first for replay."""
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_messages(self) -> int:
        conn = self._require()
        with self._lock:
            cur = conn.execute("DELETE FROM messages")
            conn.commit()
        return cur.rowcount


store = Store()

"""Store-level reminders: schema migration, recurring re-arm, claim races."""

import time

from app.services.store import store


def test_migration_is_idempotent(fresh_store):
    fresh_store._migrate_reminders()  # second run must be a no-op
    fresh_store._migrate_reminders()
    columns = {
        row["name"] for row in fresh_store._require().execute("PRAGMA table_info(reminders)")
    }
    assert {"repeat", "power_action"} <= columns


def test_add_and_due_round_trip(fresh_store):
    fire = time.time() + 3600
    row = store.add_reminder("water plants", fire, repeat="daily")
    assert row["repeat"] == "daily" and row["power_action"] is None
    due = store.due_reminders(time.time() + 7200)
    assert [r["id"] for r in due] == [row["id"]]
    assert due[0]["text"] == "water plants"


def test_due_excludes_future_and_fired(fresh_store):
    future = store.add_reminder("later", time.time() + 3600)
    store.add_reminder("now", time.time() - 1)
    assert all(r["id"] != future["id"] for r in store.due_reminders())
    store.mark_fired(future["id"])
    assert all(r["id"] != future["id"] for r in store.due_reminders(time.time() + 7200))


def test_mark_fired_is_atomic(fresh_store):
    row = store.add_reminder("race", time.time() - 1)
    assert store.mark_fired(row["id"]) is True
    assert store.mark_fired(row["id"]) is False  # second claim loses


def test_rearm_recurring_daily(fresh_store):
    row = store.add_reminder("daily", time.time() - 1, repeat="daily")
    store.mark_fired(row["id"])
    store.rearm_recurring(row["id"], "daily", row["fire_time"])
    after = store.due_reminders(time.time() + 90000)
    assert [r["id"] for r in after] == [row["id"]]
    assert after[0]["fire_time"] == row["fire_time"] + 86400


def test_rearm_recurring_weekly(fresh_store):
    row = store.add_reminder("weekly", time.time() - 1, repeat="weekly")
    store.mark_fired(row["id"])
    store.rearm_recurring(row["id"], "weekly", row["fire_time"])
    after = store.due_reminders(time.time() + 7 * 90000)
    assert after[0]["fire_time"] == row["fire_time"] + 7 * 86400


def test_rearm_requires_fired_claim(fresh_store):
    row = store.add_reminder("daily", time.time() - 1, repeat="daily")
    store.rearm_recurring(row["id"], "daily", row["fire_time"])
    after = store.due_reminders(time.time() + 90000)
    assert after[0]["fire_time"] == row["fire_time"]  # untouched


def test_unmark_fired_rearms_for_retry(fresh_store):
    row = store.add_reminder("shutdown", time.time() - 1, power_action="shutdown")
    assert store.mark_fired(row["id"]) is True
    assert store.unmark_fired(row["id"]) is True
    due = store.due_reminders(time.time())
    assert [r["id"] for r in due] == [row["id"]]
    assert due[0]["power_action"] == "shutdown"


def test_unmark_fired_respects_claim(fresh_store):
    row = store.add_reminder("shutdown", time.time() - 1, power_action="shutdown")
    assert store.unmark_fired(row["id"]) is False  # never claimed — no re-arm

    store.mark_fired(row["id"])
    assert store.unmark_fired(row["id"], retry_at=12345.0) is True
    due = store.due_reminders(12345.0)
    assert due and due[0]["fire_time"] == 12345.0


def test_pending_reminders_shape(fresh_store):
    store.add_reminder("a", time.time() + 60)
    store.add_reminder("b", time.time() + 120, repeat="weekly")
    pending = store.pending_reminders()
    assert len(pending) == 2
    assert pending[0]["fire_time"] <= pending[1]["fire_time"]
    assert pending[1]["repeat"] == "weekly"
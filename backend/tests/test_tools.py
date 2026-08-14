"""Tool layer: dispatch never raises, and the model-facing validators return
speakable ERROR strings instead of exceptions. The PC-action tools themselves
are never invoked here — only the scheduling/validation halves."""

import pytest

from app.services import tools
from app.services.reminders import reminder_service


async def test_execute_unknown_tool_never_raises(fresh_store):
    result = await tools.execute("no_such_tool", {})
    assert isinstance(result, str) and result.startswith("ERROR:")


async def test_execute_missing_args_never_raises(fresh_store):
    result = await tools.execute("set_reminder", None)
    assert isinstance(result, str) and result.startswith("ERROR:")


async def test_set_reminder_validations(fresh_store):
    cases = [
        ({"text": ""}, "text"),
        ({"text": "x", "at": "25:00"}, "valid time"),
        ({"text": "x", "at": "not a time"}, "HH:MM"),
        ({"text": "x", "minutes_from_now": -5}, "negative"),
        ({"text": "x", "minutes_from_now": "soon"}, "whole number"),
        ({"text": "x", "repeat": "yearly", "minutes_from_now": 5}, "daily"),
        ({"text": "x"}, "either"),
    ]
    for args, needle in cases:
        result = await tools.execute("set_reminder", args)
        assert "ERROR" in result, (args, result)
        assert needle.lower() in result.lower(), (args, result)


async def test_set_reminder_relative_success(fresh_store):
    result = await tools.execute(
        "set_reminder", {"text": "water plants", "minutes_from_now": 30}
    )
    assert result.startswith("Reminder set: 'water plants' in 30 minute(s)")
    assert len(reminder_service.pending()) == 1


async def test_set_reminder_repeat_suffix(fresh_store):
    result = await tools.execute(
        "set_reminder", {"text": "yoga", "minutes_from_now": 15, "repeat": "daily"}
    )
    assert result.startswith("Reminder set: 'yoga' in 15 minute(s)")
    assert "repeating daily" in result


async def test_set_reminder_at_clock_time(fresh_store):
    result = await tools.execute("set_reminder", {"text": "meditate", "at": "19:30"})
    assert result.startswith("Reminder set: 'meditate' at 7:30 PM")


async def test_set_reminder_at_with_repeat(fresh_store):
    result = await tools.execute(
        "set_reminder", {"text": "coffee", "at": "07:00", "repeat": "daily"}
    )
    assert "7:00 AM" in result and "repeating daily" in result


def test_next_occurrence_is_future_with_right_clock(fresh_store):
    from datetime import datetime

    from app.services.tools import _next_occurrence

    ts = _next_occurrence(7, 30)
    dt = datetime.fromtimestamp(ts)
    assert (dt.hour, dt.minute) == (7, 30)
    assert ts > datetime.now().timestamp()


async def test_schedule_power_validations(fresh_store):
    cases = [
        ({"action": "fly", "minutes_from_now": 5}, "sleep, shutdown, lock"),
        ({"action": "lock", "minutes_from_now": 0}, "1 and 4320"),
        ({"action": "lock", "minutes_from_now": 5000}, "1 and 4320"),
        ({"action": "lock", "minutes_from_now": "soon"}, "whole number"),
    ]
    for args, needle in cases:
        result = await tools.execute("schedule_power_action", args)
        assert "ERROR" in result, (args, result)
        assert needle in result, (args, result)


async def test_schedule_power_creates_power_row(fresh_store):
    result = await tools.execute(
        "schedule_power_action", {"action": "lock", "minutes_from_now": 30}
    )
    assert result.startswith("Scheduled lock for 30 minute(s)")
    pending = reminder_service.pending()
    assert len(pending) == 1
    assert pending[0]["power_action"] == "lock"
    assert pending[0]["text"] == "Scheduled lock"


def test_ps_literal_escapes_single_quotes():
    from app.services.pc_control import _ps_literal

    assert _ps_literal("plain") == "'plain'"
    assert _ps_literal("O'Brien") == "'O''Brien'"
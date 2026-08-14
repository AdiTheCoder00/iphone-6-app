"""The deterministic fast paths are the thinnest slice of the companion, so
they get the tightest tests: a regression here is a wrong-sentence-on-screen
bug, and one of them ("lock it") used to crash a whole chat turn."""

import re

from app.services.companion import (
    _COMMAND_RE,
    _FAST_LOCK_RE,
    _FAST_TIMER_BARE_RE,
    _FAST_TIMER_RE,
)


def _matches(pattern: re.Pattern, text: str) -> bool:
    return bool(pattern.match(text))


# --- lock: deliberately tight -----------------------------------------------
def test_lock_plain():
    assert _matches(_FAST_LOCK_RE, "lock")


def test_lock_with_target():
    assert _matches(_FAST_LOCK_RE, "please lock the pc")
    assert _matches(_FAST_LOCK_RE, "lock my screen")


def test_lock_it_does_not_match():
    # "lock it" has no match — it must fall through to the LLM, not crash.
    assert not _matches(_FAST_LOCK_RE, "lock it")


def test_lock_other_verbs_do_not_match():
    assert not _matches(_FAST_LOCK_RE, "unlock")
    assert not _matches(_FAST_LOCK_RE, "lockdown")


# --- timer patterns ---------------------------------------------------------
def test_timer_set_a_one_minute_timer():
    m = _FAST_TIMER_RE.match("set a 1 minute timer")
    assert m and m.group(1) == "1"


def test_timer_with_label():
    m = _FAST_TIMER_RE.match("set a 10 minute timer for pasta")
    assert m and m.group(1) == "10" and m.group(2).strip() == "pasta"


def test_timer_trailing_please():
    assert _matches(_FAST_TIMER_RE, "set a 5 minute timer please")


def test_timer_bare_order():
    assert _matches(_FAST_TIMER_BARE_RE, "timer 15 minutes")
    assert _matches(_FAST_TIMER_BARE_RE, "please set a timer for 2 minutes")


def test_timer_hours_not_supported():
    # Hours deliberately do not match — the LLM path explains itself instead.
    assert not _matches(_FAST_TIMER_RE, "set a 2 hour timer")
    assert not _matches(_FAST_TIMER_BARE_RE, "timer 90 seconds")


def test_timer_rejects_implausible_durations():
    # 3 digits match (the clamp to 180 is TimerService's job, see
    # test_timers.py::test_add_clamps_bounds); 4 digits must not fast-path.
    assert not _matches(_FAST_TIMER_RE, "set a 2000 minute timer")
    assert not _matches(_FAST_TIMER_BARE_RE, "timer 2000 minutes")


# --- command history skip ---------------------------------------------------
def test_command_re_matches_commands():
    assert _COMMAND_RE.search("lock it")
    assert _COMMAND_RE.search("what time is it")
    assert _COMMAND_RE.search("set a timer for 5 minutes")


def test_command_re_ignores_plain_smalltalk():
    assert not _COMMAND_RE.search("good morning")
    assert not _COMMAND_RE.search("tell me a joke")
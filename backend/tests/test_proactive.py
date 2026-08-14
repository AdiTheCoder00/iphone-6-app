"""Quiet-hours window logic — pure datetime math, no network or timers."""

from datetime import datetime

from app.services.proactive import ProactiveService


def _service(settings):
    return ProactiveService()


def test_quiet_hours_disabled_when_start_equals_end(isolated_settings):
    isolated_settings.proactive_quiet_start = 0
    isolated_settings.proactive_quiet_end = 0
    svc = ProactiveService()
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 12, 0)) is False
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 0, 0)) is False


def test_quiet_hours_same_day_window(isolated_settings):
    isolated_settings.proactive_quiet_start = 13
    isolated_settings.proactive_quiet_end = 14
    svc = ProactiveService()
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 13, 30)) is True
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 13, 0)) is True   # start inclusive
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 14, 0)) is False  # end exclusive
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 12, 59)) is False


def test_quiet_hours_midnight_wrap(isolated_settings):
    isolated_settings.proactive_quiet_start = 22
    isolated_settings.proactive_quiet_end = 7
    svc = ProactiveService()
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 23, 0)) is True
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 22, 0)) is True   # start inclusive
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 6, 59)) is True
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 7, 0)) is False  # end exclusive
    assert svc._in_quiet_hours(datetime(2026, 8, 15, 12, 0)) is False
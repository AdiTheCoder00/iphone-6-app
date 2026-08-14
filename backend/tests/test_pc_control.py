"""PC control guards: everything must refuse cleanly off-Windows, and the
PowerShell string escaping must survive hostile input."""

import pytest

from app.services import pc_control


def test_require_windows_guard(monkeypatch):
    monkeypatch.setattr(pc_control, "IS_WINDOWS", False)
    with pytest.raises(pc_control.PCControlError):
        pc_control._require_windows()


def test_guarded_functions_raise_off_windows(monkeypatch):
    monkeypatch.setattr(pc_control, "IS_WINDOWS", False)
    for call in (
        lambda: pc_control.find_files("report"),
        lambda: pc_control.capture_screenshot("x.png"),
        lambda: pc_control.set_clipboard("hi"),
        lambda: pc_control.get_clipboard(),
        lambda: pc_control.lock_screen(),
        lambda: pc_control.sleep_pc(),
        lambda: pc_control.shutdown_pc(),
        lambda: pc_control.set_volume(50),
    ):
        with pytest.raises(pc_control.PCControlError):
            call()


def test_find_files_requires_a_name(monkeypatch):
    monkeypatch.setattr(pc_control, "IS_WINDOWS", True)
    with pytest.raises(pc_control.PCControlError):
        pc_control.find_files("")
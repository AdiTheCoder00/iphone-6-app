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


def _patch_open(monkeypatch):
    """Patch the side effects of open_url and record what the browser
    would have been handed."""
    monkeypatch.setattr(pc_control, "IS_WINDOWS", True)
    monkeypatch.setattr(pc_control, "_grant_foreground", lambda: None)
    monkeypatch.setattr(pc_control, "_foreground_in_background", lambda fragments: None)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    return opened


def test_open_url_accepts_valid_addresses(monkeypatch):
    opened = _patch_open(monkeypatch)
    cases = {
        "https://youtube.com/watch?v=1": "https://youtube.com/watch?v=1",
        "http://example.com": "http://example.com",
        # Bare hostname: validated before https is prepended.
        "youtube.com": "https://youtube.com",
        "www.example.com/path?q=1": "https://www.example.com/path?q=1",
    }
    for given, expected in cases.items():
        assert pc_control.open_url(given) == expected
    assert opened == list(cases.values())


def test_open_url_refuses_hostile_input(monkeypatch):
    _patch_open(monkeypatch)
    bad = {
        "": "no address given",
        # Schemes outside http/https — the injection paths.
        "javascript:alert(1)": "only http and https",
        "file:///C:/Windows/notepad.exe": "only http and https",
        "ms-settings:display": "only http and https",
        # "C:/..." parses with scheme "c".
        "C:/Windows/notepad.exe": "only http and https",
        # A bare host:port parses as scheme "localhost" (netloc empty) and is
        # rejected by the scheme gate before it could ever be opened.
        "localhost:8080": "only http and https",
        "notepad.exe": "looks like a program",
        "calc.exe": "looks like a program",
    }
    for given, message in bad.items():
        with pytest.raises(pc_control.PCControlError, match=message):
            pc_control.open_url(given)
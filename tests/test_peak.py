from datetime import datetime

from backend.agent.model import in_peak_window

WINDOWS = ["18:00-21:00", "23:00-03:00"]


def at(hour, minute=0):
    return datetime(2026, 7, 3, hour, minute)


def test_inside_evening_window():
    assert in_peak_window(at(18, 0), WINDOWS)
    assert in_peak_window(at(20, 59), WINDOWS)


def test_outside_windows():
    assert not in_peak_window(at(17, 59), WINDOWS)
    assert not in_peak_window(at(21, 0), WINDOWS)
    assert not in_peak_window(at(12, 0), WINDOWS)


def test_midnight_crossing_window():
    assert in_peak_window(at(23, 0), WINDOWS)
    assert in_peak_window(at(0, 30), WINDOWS)
    assert in_peak_window(at(2, 59), WINDOWS)
    assert not in_peak_window(at(3, 0), WINDOWS)
    assert not in_peak_window(at(22, 59), WINDOWS)


def test_confirmation_flow(tmp_env):
    from backend.agent.model import confirm_peak, peak_confirmed

    assert not peak_confirmed(12345)
    confirm_peak(12345)
    assert peak_confirmed(12345)

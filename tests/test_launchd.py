"""Pure-logic tests — no live launchctl, no machine state (fixtures only)."""

from datetime import datetime

from app import launchd


def test_humanize_weekly():
    plist = {"StartCalendarInterval": {"Hour": 10, "Minute": 0, "Weekday": 0}}
    assert launchd.humanize_schedule(plist) == "Sun 10:00"


def test_humanize_daily_and_interval():
    assert launchd.humanize_schedule({"StartCalendarInterval": {"Hour": 18, "Minute": 30}}) == "Daily 18:30"
    assert launchd.humanize_schedule({"StartInterval": 3600}) == "Every 1h"
    assert launchd.humanize_schedule({"StartInterval": 300}) == "Every 5m"


def test_humanize_fallbacks():
    assert launchd.humanize_schedule({"RunAtLoad": True}) == "At login"
    assert launchd.humanize_schedule({"KeepAlive": True}) == "Always on"
    assert launchd.humanize_schedule({"WatchPaths": ["/tmp/x"]}) == "On file change"
    assert launchd.humanize_schedule({}) == "Manual / on-demand"


def test_humanize_multi_interval_dedupes():
    plist = {"StartCalendarInterval": [
        {"Hour": 9, "Minute": 0, "Weekday": 1},
        {"Hour": 9, "Minute": 0, "Weekday": 3},
    ]}
    assert launchd.humanize_schedule(plist) == "Mon 09:00, Wed 09:00"


def test_next_run_sunday_1000():
    # Thu 2026-07-02 12:00 -> next Sunday 10:00 is 2026-07-05 10:00
    now = datetime(2026, 7, 2, 12, 0)
    nr = launchd.next_run({"StartCalendarInterval": {"Hour": 10, "Minute": 0, "Weekday": 0}}, now=now)
    assert nr == datetime(2026, 7, 5, 10, 0)


def test_next_run_daily_rolls_to_tomorrow():
    now = datetime(2026, 7, 2, 19, 0)  # already past 18:30 today
    nr = launchd.next_run({"StartCalendarInterval": {"Hour": 18, "Minute": 30}}, now=now)
    assert nr == datetime(2026, 7, 3, 18, 30)


def test_next_run_none_for_non_calendar():
    assert launchd.next_run({"StartInterval": 3600}) is None
    assert launchd.next_run({"RunAtLoad": True}) is None


def test_parse_launchctl_list():
    out = '{\n\t"PID" = 4321;\n\t"LastExitStatus" = 0;\n\t"Label" = "x";\n};'
    parsed = launchd.parse_launchctl_list(out)
    assert parsed == {"pid": 4321, "last_exit": 0}


def test_parse_launchctl_list_idle_nonzero_exit():
    out = '{\n\t"LastExitStatus" = 256;\n\t"OnDemand" = true;\n};'
    parsed = launchd.parse_launchctl_list(out)
    assert parsed == {"last_exit": 256}
    assert "pid" not in parsed


def test_is_vendor():
    assert launchd.is_vendor("com.google.keystone.agent")
    assert not launchd.is_vendor("com.groceryhelper.recipes")

from datetime import datetime

from birthday_bot.runner import RunResult
from birthday_bot.scheduler import Scheduler, normalize_time
from birthday_bot.settings import Settings, State

DAY = datetime(2026, 11, 6)


class FakeRunner:
    busy = False

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def run(self, reason="", dry_run=None):
        self.calls.append(reason)
        return RunResult(ok=self.ok, message="test")


def make(tmp_path, times, catch_up=True, ok=True):
    s = Settings(tmp_path / "settings.json")
    s.set("schedule_times", times)
    s.set("catch_up", catch_up)
    st = State(tmp_path / "state.json")
    r = FakeRunner(ok)
    return Scheduler(s, st, r), r, st


def test_normalize_time():
    assert normalize_time("9:00") == "09:00"
    assert normalize_time("23:59") == "23:59"
    assert normalize_time("24:00") is None
    assert normalize_time("7:5") is None


def test_missed_time_runs_once_later_same_day(tmp_path):
    sc, r, _ = make(tmp_path, ["09:00"])
    sc.tick(DAY.replace(hour=8))
    assert r.calls == []
    sc.tick(DAY.replace(hour=14, minute=30))   # computer came back on
    assert len(r.calls) == 1 and "catch-up" in r.calls[0]
    sc.tick(DAY.replace(hour=14, minute=31))
    assert len(r.calls) == 1
    assert sc.next_run(DAY.replace(hour=15)) == datetime(2026, 11, 7, 9, 0)


def test_several_missed_times_collapse_into_one_run(tmp_path):
    sc, r, st = make(tmp_path, ["09:00", "12:00", "18:00"])
    sc.tick(DAY.replace(hour=13))
    assert len(r.calls) == 1
    assert set(st.completed_slots("2026-11-06")) == {"09:00", "12:00"}
    sc.tick(DAY.replace(hour=18, second=5))
    assert r.calls[-1] == "scheduled 18:00"


def test_catch_up_off_skips_stale_times(tmp_path):
    sc, r, st = make(tmp_path, ["09:00"], catch_up=False)
    sc.tick(DAY.replace(hour=14))
    assert r.calls == []
    assert st.completed_slots("2026-11-06") == {"09:00": "missed"}


def test_failure_schedules_retry(tmp_path):
    sc, r, _ = make(tmp_path, ["09:00"], ok=False)
    sc.tick(DAY.replace(hour=9, second=10))
    assert len(r.calls) == 1 and sc.retry_at is not None


def test_new_day_runs_again(tmp_path):
    sc, r, _ = make(tmp_path, ["09:00"])
    sc.tick(DAY.replace(hour=10))
    sc.tick(datetime(2026, 11, 7, 10))
    assert len(r.calls) == 2

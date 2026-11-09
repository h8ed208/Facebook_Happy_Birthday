"""Daily scheduler with catch-up for missed run times.

Every scheduled time is a "slot" for the day. The loop wakes every few seconds and
asks: is there a slot for today whose time has passed and that hasn't run yet? If so,
it runs the job once (several missed slots collapse into one run). Completed slots are
stored on disk, so if the computer was off or asleep at 9:00, the job runs as soon as
the app is running again later that day. Birthdays only show on the day itself, so
missed slots never carry over past midnight.
"""
from __future__ import annotations

import logging
import random
import re
import threading
from datetime import datetime, timedelta

log = logging.getLogger("birthday_bot")

TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def normalize_time(text: str) -> str | None:
    m = TIME_RE.match(text or "")
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    if h > 23 or mnt > 59:
        return None
    return f"{h:02d}:{mnt:02d}"


def parse_times(times) -> list[str]:
    return sorted({t for t in (normalize_time(x) for x in times or []) if t})


def slot_dt(day: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


class Scheduler:
    TICK_SECONDS = 15

    def __init__(self, settings, state, runner, on_event=None):
        self.settings = settings
        self.state = state
        self.runner = runner
        self.on_event = on_event or (lambda *a: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.retry_at: datetime | None = None

    # --- public ------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        """Re-evaluate right away (e.g. after settings change)."""
        self._wake.set()

    def due_slots(self, now: datetime) -> list[str]:
        cfg = self.settings.snapshot()
        if not cfg.get("schedule_enabled", True):
            return []
        today = now.date().isoformat()
        done = self.state.completed_slots(today)
        due = [t for t in parse_times(cfg.get("schedule_times")) if slot_dt(now, t) <= now and t not in done]
        if not cfg.get("catch_up", True) and due:
            grace = timedelta(minutes=int(cfg.get("grace_minutes", 10)))
            stale = [t for t in due if now - slot_dt(now, t) > grace]
            if stale:
                log.info("Missed run time(s) %s – catch-up is off, skipping.", ", ".join(stale))
                self.state.mark_slots(today, stale, "missed")
            due = [t for t in due if t not in stale]
        return due

    def next_run(self, now: datetime | None = None) -> datetime | None:
        now = now or datetime.now()
        cfg = self.settings.snapshot()
        if not cfg.get("schedule_enabled", True):
            return None
        times = parse_times(cfg.get("schedule_times"))
        if not times:
            return None
        if self.retry_at and self.retry_at.date() == now.date():
            return self.retry_at
        if self.due_slots(now):
            return now
        done = self.state.completed_slots(now.date().isoformat())
        for t in times:
            if slot_dt(now, t) > now and t not in done:
                return slot_dt(now, t)
        return slot_dt(now + timedelta(days=1), times[0])

    # --- loop -------------------------------------------------------------
    def _loop(self):
        log.debug("Scheduler started")
        while not self._stop.is_set():
            try:
                self.tick(datetime.now())
            except Exception:
                log.exception("Scheduler error")
            self._wake.wait(self.TICK_SECONDS)
            self._wake.clear()

    def tick(self, now: datetime):
        if self.retry_at and self.retry_at.date() != now.date():
            self.retry_at = None  # new day – yesterday's retries no longer matter
        if self.retry_at and now < self.retry_at:
            return
        due = self.due_slots(now)
        if not due:
            return
        if self.runner.busy:
            return

        latest = max(due)
        late_by = now - slot_dt(now, latest)
        if self.retry_at:
            reason = "retry"
        elif late_by > timedelta(minutes=2):
            reason = f"catch-up for {', '.join(due)}"
        else:
            reason = f"scheduled {latest}"
        result = self.runner.run(reason=reason)
        if result.busy:
            return

        today = now.date().isoformat()
        if result.ok:
            self.state.mark_slots(today, due, "done")
            self.state.reset_fail(today)
            self.retry_at = None
        else:
            cfg = self.settings.snapshot()
            fails = self.state.inc_fail(today)
            if fails > int(cfg.get("max_retries", 3)):
                log.error("Giving up for today after %d failed attempts.", fails)
                self.state.mark_slots(today, due, "failed")
                self.retry_at = None
            else:
                self.retry_at = datetime.now() + timedelta(minutes=int(cfg.get("retry_minutes", 20)))
                log.info("Will retry at %s.", self.retry_at.strftime("%H:%M"))
        self.on_event("schedule", None)


class PokeScheduler:
    """Checks the pokes page every N minutes (with a little randomness) while enabled."""

    TICK_SECONDS = 10
    JITTER = 0.2

    def __init__(self, settings, runner, on_event=None):
        self.settings = settings
        self.runner = runner
        self.on_event = on_event or (lambda *a: None)
        self.next_at: datetime | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="poke-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        self._wake.set()

    def interval(self) -> timedelta:
        minutes = max(2, int(self.settings.get("poke_interval_minutes") or 10))
        return timedelta(minutes=minutes * random.uniform(1 - self.JITTER, 1 + self.JITTER))

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick(datetime.now())
            except Exception:
                log.exception("Poke scheduler error")
            self._wake.wait(self.TICK_SECONDS)
            self._wake.clear()

    def tick(self, now: datetime):
        if not self.settings.get("poke_enabled"):
            self.next_at = None
            return
        if self.next_at is None:
            self.next_at = now + timedelta(seconds=15)  # first check shortly after enabling/starting
            self.on_event("poke_schedule", None)
            return
        cap = now + timedelta(minutes=max(2, int(self.settings.get("poke_interval_minutes") or 10)) * 1.25)
        if self.next_at > cap:
            self.next_at = now + self.interval()  # interval was shortened
        if now < self.next_at:
            return
        if self.runner.busy:
            self.next_at = now + timedelta(seconds=60)  # a birthday run is going; try again shortly
            return
        result = self.runner.poke(reason="auto")
        if result.busy:
            self.next_at = datetime.now() + timedelta(seconds=60)
        else:
            self.next_at = datetime.now() + self.interval()
        self.on_event("poke_schedule", None)

"""Persistent settings and run-state storage (JSON files in the user's app-data folder)."""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

APP_NAME = "FBBirthdayBot"


def data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        d = base / APP_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        d = base / "fb-birthday-bot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def browser_profile_dir() -> Path:
    d = data_dir() / "browser_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def debug_dir() -> Path:
    d = data_dir() / "debug"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_SETTINGS = {
    # Messages
    "templates": [
        "Happy birthday, {first_name}! 🎉 Hope you have an awesome day!",
    ],
    "custom_messages": {},          # {"Full Name": "message"} – per-friend override
    "excluded": [],                 # names (or profile URL fragments) never to message
    # Schedule
    "schedule_enabled": True,
    "schedule_times": ["09:00"],    # 24h HH:MM, local time
    "catch_up": True,               # run missed times later the same day
    "grace_minutes": 10,            # only used when catch_up is off
    "retry_minutes": 20,
    "max_retries": 3,
    # Sending
    "send_mode": "messenger",       # "messenger" or "timeline"
    "fallback_to_messenger": True,  # if timeline posting fails, try Messenger
    "dry_run": True,                # preview mode: find birthdays but don't send
    "delay_min_sec": 20,
    "delay_max_sec": 60,
    "max_per_run": 25,
    # Auto-poker
    "poke_enabled": False,
    "poke_interval_minutes": 10,
    "poke_mode": "everyone",        # "everyone" (except ignored) or "selected" (only people set to Auto)
    "poke_rules": {},               # {profile URL or lowercase name: "auto" | "ignore"}
    "poke_headless": True,          # run poke checks without a visible browser window
    "poke_max_per_check": 20,
    # Browser
    "headless": False,
    "browser_channel": "chromium",  # "chromium", "chrome" or "msedge"
    # App behaviour
    "autostart": False,
    "start_minimized": False,
    "minimize_on_close": True,
}


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class JsonStore:
    def __init__(self, path: Path, defaults: dict):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(defaults)
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, json.JSONDecodeError):
                # Keep a copy of the broken file and start fresh
                try:
                    os.replace(path, path.with_suffix(".corrupt.json"))
                except OSError:
                    pass

    def save(self) -> None:
        with self.lock:
            _atomic_write(self.path, self.data)


class Settings(JsonStore):
    def __init__(self, path: Path | None = None):
        super().__init__(path or (data_dir() / "settings.json"), DEFAULT_SETTINGS)

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.data.get(key, DEFAULT_SETTINGS.get(key)))

    def set(self, key, value, save: bool = True):
        with self.lock:
            self.data[key] = value
            if save:
                self.save()

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)


class State(JsonStore):
    """Tracks which scheduled slots ran and who was messaged on which day."""

    KEEP_DAYS = 400

    def __init__(self, path: Path | None = None):
        super().__init__(path or (data_dir() / "state.json"),
                         {"slots": {}, "sent": {}, "fails": {}, "last_run": None, "last_result": "",
                          "pokers": {}, "last_poke_check": None, "last_poke_result": ""})
        self.prune()

    # --- slots -----------------------------------------------------------
    def completed_slots(self, day: str) -> dict:
        with self.lock:
            return dict(self.data["slots"].get(day, {}))

    def mark_slots(self, day: str, slots, status: str = "done") -> None:
        with self.lock:
            d = self.data["slots"].setdefault(day, {})
            for s in slots:
                d[s] = status
            self.save()

    def inc_fail(self, day: str) -> int:
        with self.lock:
            n = self.data["fails"].get(day, 0) + 1
            self.data["fails"][day] = n
            self.save()
            return n

    def reset_fail(self, day: str) -> None:
        with self.lock:
            if self.data["fails"].pop(day, None) is not None:
                self.save()

    # --- per-friend send log --------------------------------------------
    def sent_entry(self, day: str, key: str) -> dict | None:
        with self.lock:
            e = self.data["sent"].get(day, {}).get(key)
            return dict(e) if e else None

    def record(self, day: str, key: str, **entry) -> None:
        with self.lock:
            entry.setdefault("time", datetime.now().strftime("%H:%M:%S"))
            self.data["sent"].setdefault(day, {})[key] = entry
            self.save()

    def day_entries(self, day: str) -> dict:
        with self.lock:
            return copy.deepcopy(self.data["sent"].get(day, {}))

    def set_last_run(self, result: str) -> None:
        with self.lock:
            self.data["last_run"] = datetime.now().isoformat(timespec="seconds")
            self.data["last_result"] = result
            self.save()

    # --- pokes ---------------------------------------------------------------
    def upsert_poker(self, key: str, name: str, url: str, text: str = "") -> None:
        with self.lock:
            p = self.data["pokers"].setdefault(key, {"name": name, "url": url, "poked_back": 0})
            p.update(name=name or p.get("name", ""), url=url or p.get("url", ""),
                     last_seen=datetime.now().isoformat(timespec="seconds"), last_text=text[:200])
            self.save()

    def record_poke_back(self, key: str) -> None:
        with self.lock:
            p = self.data["pokers"].get(key)
            if p is not None:
                p["poked_back"] = p.get("poked_back", 0) + 1
                p["last_poked_back"] = datetime.now().isoformat(timespec="seconds")
                self.save()

    def pokers(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data["pokers"])

    def forget_poker(self, key: str) -> None:
        with self.lock:
            if self.data["pokers"].pop(key, None) is not None:
                self.save()

    def set_last_poke(self, result: str) -> None:
        with self.lock:
            self.data["last_poke_check"] = datetime.now().isoformat(timespec="seconds")
            self.data["last_poke_result"] = result
            self.save()

    def prune(self) -> None:
        cutoff = (date.today() - timedelta(days=self.KEEP_DAYS)).isoformat()
        with self.lock:
            for section in ("slots", "sent", "fails"):
                for k in [k for k in self.data[section] if k < cutoff]:
                    del self.data[section][k]

"""The birthday-check job: find today's birthdays and send messages."""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass, field
from datetime import date

from . import settings as S
from .fb_client import FacebookClient, FBError, Friend, NotLoggedIn, load_selectors

log = logging.getLogger("birthday_bot")


@dataclass
class RunResult:
    ok: bool
    busy: bool = False
    found: int = 0
    sent: int = 0
    previewed: int = 0
    skipped: int = 0
    failed: int = 0
    message: str = ""
    friends: list = field(default_factory=list)


def render_message(template: str, friend: Friend) -> str:
    values = {
        "name": friend.name,
        "first_name": friend.first_name,
        "last_name": friend.last_name,
    }
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out.strip()


def choose_message(cfg: dict, friend: Friend) -> str:
    custom = {k.strip().lower(): v for k, v in (cfg.get("custom_messages") or {}).items()}
    if friend.name.lower() in custom and custom[friend.name.lower()].strip():
        template = custom[friend.name.lower()]
    else:
        templates = [t for t in cfg.get("templates") or [] if t.strip()]
        template = random.choice(templates) if templates else "Happy birthday, {first_name}! 🎉"
    return render_message(template, friend)


def is_excluded(cfg: dict, friend: Friend) -> bool:
    for item in cfg.get("excluded") or []:
        item = item.strip().lower()
        if not item:
            continue
        if item == friend.name.lower() or (len(item) > 4 and item in friend.url.lower()):
            return True
    return False


class BirthdayRunner:
    def __init__(self, settings: S.Settings, state: S.State, on_event=None):
        self.settings = settings
        self.state = state
        self.on_event = on_event or (lambda *a: None)
        self._lock = threading.Lock()
        self.stop_event = threading.Event()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _client(self, headless: bool | None = None) -> FacebookClient:
        cfg = self.settings.snapshot()
        return FacebookClient(
            profile_dir=S.browser_profile_dir(),
            selectors=load_selectors(S.data_dir() / "selectors.json"),
            headless=cfg["headless"] if headless is None else headless,
            channel=cfg.get("browser_channel") or "chromium",
            debug_dir=S.debug_dir(),
        )

    # --- login -------------------------------------------------------------
    def login(self) -> bool:
        if not self._lock.acquire(blocking=False):
            log.warning("Busy with another task – try again in a moment.")
            return False
        try:
            log.info("Opening a browser window – log in to Facebook there (the window closes by itself).")
            with self._client(headless=False) as fb:
                ok = fb.interactive_login(stop=self.stop_event)
            log.info("Login %s.", "saved" if ok else "not completed")
            self.on_event("login", ok)
            return ok
        except FBError as e:
            log.error("%s", e)
            return False
        except Exception:
            log.exception("Login failed")
            return False
        finally:
            self._lock.release()

    def check_login(self) -> bool | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            with self._client(headless=True) as fb:
                ok = fb.is_logged_in()
            self.on_event("login", ok)
            return ok
        except FBError as e:
            log.error("%s", e)
            self.on_event("login", "error")
            return None
        except Exception:
            log.exception("Login check failed")
            self.on_event("login", "error")
            return None
        finally:
            self._lock.release()

    def debug_snapshot(self):
        if not self._lock.acquire(blocking=False):
            log.warning("Busy – try again in a moment.")
            return None
        try:
            with self._client() as fb:
                from .fb_client import BIRTHDAYS_URL
                fb._goto(BIRTHDAYS_URL)
                path = fb.snapshot("manual_debug")
            log.info("Saved debug snapshot: %s", path)
            return path
        except Exception as e:
            log.error("Snapshot failed: %s", e)
            return None
        finally:
            self._lock.release()

    # --- main job -------------------------------------------------------------
    def run(self, dry_run: bool | None = None, reason: str = "manual") -> RunResult:
        if not self._lock.acquire(blocking=False):
            return RunResult(ok=False, busy=True, message="Another run is in progress")
        self.stop_event.clear()
        self.on_event("run_start", reason)
        try:
            res = self._run(dry_run, reason)
        except NotLoggedIn as e:
            res = RunResult(ok=False, message=str(e))
            self.on_event("login", False)
        except FBError as e:
            res = RunResult(ok=False, message=str(e))
        except Exception as e:
            log.exception("Unexpected error during run")
            res = RunResult(ok=False, message=f"Unexpected error: {e}")
        finally:
            self._lock.release()
        if not res.ok:
            log.error("Run failed: %s", res.message)
        self.state.set_last_run(res.message)
        self.on_event("run_done", res)
        return res

    def _run(self, dry_run, reason) -> RunResult:
        cfg = self.settings.snapshot()
        dry = cfg["dry_run"] if dry_run is None else dry_run
        today = date.today().isoformat()
        log.info("── Birthday check (%s)%s ──", reason, " – PREVIEW, nothing will be sent" if dry else "")
        res = RunResult(ok=True)

        with self._client() as fb:
            if not fb.is_logged_in():
                raise NotLoggedIn("Not logged in. Click “Log in to Facebook” on the Dashboard.")
            friends = fb.todays_birthdays()
            res.found = len(friends)
            res.friends = [f.name for f in friends]
            if not friends:
                log.info("No birthdays today.")
            else:
                log.info("Birthdays today: %s", ", ".join(f.name for f in friends))

            sends_this_run = 0
            for i, friend in enumerate(friends):
                if self.stop_event.is_set():
                    log.info("Stopped.")
                    break
                prev = self.state.sent_entry(today, friend.key)
                if prev and prev.get("status") == "sent":
                    log.info("  %s – already messaged today, skipping.", friend.name)
                    res.skipped += 1
                    continue
                if is_excluded(cfg, friend):
                    log.info("  %s – on your exclude list, skipping.", friend.name)
                    self.state.record(today, friend.key, name=friend.name, url=friend.url, status="excluded")
                    res.skipped += 1
                    continue
                if sends_this_run >= int(cfg.get("max_per_run", 25)):
                    log.warning("  Reached the max-per-run limit; remaining friends will be done next run.")
                    break

                msg = choose_message(cfg, friend)
                if dry:
                    log.info("  [preview] %s ← %s", friend.name, msg.replace("\n", " ⏎ "))
                    self.state.record(today, friend.key, name=friend.name, url=friend.url,
                                      status="preview", message=msg)
                    res.previewed += 1
                    continue

                try:
                    method = fb.send(friend, msg, cfg.get("send_mode", "messenger"),
                                     cfg.get("fallback_to_messenger", True))
                    log.info("  ✔ Sent to %s via %s.", friend.name, method)
                    self.state.record(today, friend.key, name=friend.name, url=friend.url,
                                      status="sent", method=method, message=msg)
                    res.sent += 1
                except NotLoggedIn:
                    raise
                except Exception as e:
                    log.error("  ✘ Couldn't message %s: %s", friend.name, e)
                    fb.snapshot(f"send_failed_{friend.name}")
                    self.state.record(today, friend.key, name=friend.name, url=friend.url,
                                      status="failed", error=str(e), message=msg)
                    res.failed += 1
                sends_this_run += 1
                self.on_event("progress", None)

                if i < len(friends) - 1:
                    lo, hi = sorted((int(cfg.get("delay_min_sec", 20)), int(cfg.get("delay_max_sec", 60))))
                    self.stop_event.wait(random.uniform(lo, hi))

        res.ok = res.failed == 0
        parts = [f"{res.found} birthday(s)"]
        if res.sent:
            parts.append(f"{res.sent} sent")
        if res.previewed:
            parts.append(f"{res.previewed} previewed")
        if res.skipped:
            parts.append(f"{res.skipped} skipped")
        if res.failed:
            parts.append(f"{res.failed} failed")
        res.message = ", ".join(parts)
        log.info("Done: %s.", res.message)
        return res

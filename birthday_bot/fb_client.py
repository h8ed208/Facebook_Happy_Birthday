"""Facebook browser automation via Playwright.

Facebook changes its page structure often. Everything that depends on the page
layout lives in DEFAULT_SELECTORS, which is copied to selectors.json in the data
folder on first run so it can be adjusted without touching the code.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("birthday_bot")

FB = "https://www.facebook.com"
BIRTHDAYS_URL = FB + "/events/birthdays/"
LOGIN_URL = FB + "/login"

DEFAULT_SELECTORS = {
    # Section headings on the birthdays page (regex, case-insensitive)
    "today_heading_regex": r"^today['’]?s birthdays?$|^birthdays today$",
    "other_heading_regex": r"^(recent|upcoming|belated|later)\b.*birthdays?|^birthdays? (this|next)",
    # Profile page "Message" button (CSS fallback; role-based lookup is tried first)
    "message_button_names": ["Message", "Send message"],
    "message_button_css": "div[role='button'][aria-label='Message'], a[aria-label='Message']",
    # Chat composer box (in the profile chat pop-up or on /messages/t/...)
    "chat_textbox": "div[role='textbox'][contenteditable='true'][aria-label*='message' i]",
    "close_chat": "[aria-label='Close chat']",
    # Write-on-timeline box inside a birthday card
    "timeline_textbox": "div[role='textbox'][contenteditable='true']",
    # Text that means Messenger wants an end-to-end-encryption PIN
    "pin_prompt_regex": r"enter your pin|restore (your )?chat history|create a pin",
}

RESERVED_PATHS = {
    "events", "groups", "pages", "watch", "marketplace", "gaming", "messages", "notifications",
    "friends", "bookmarks", "settings", "help", "photo", "photo.php", "photos", "stories", "reel",
    "reels", "hashtag", "login", "logout.php", "privacy", "policies", "ads", "business", "saved",
    "memories", "fundraisers", "home.php", "search", "story.php", "permalink.php", "share",
    "sharer", "dialog", "checkpoint", "recover", "l.php", "people", "me", "profile.php", "video",
    "videos", "live", "games", "jobs", "weather", "offers", "coronavirus_info", "feeds", "news",
}

NAME_JUNK = re.compile(r"birthday|turning|years old|today|message|write|post|see all|^\d", re.I)


class FBError(Exception):
    pass


class NotLoggedIn(FBError):
    pass


class SendError(FBError):
    pass


class PlaywrightMissing(FBError):
    pass


def load_selectors(path: Path) -> dict:
    sel = dict(DEFAULT_SELECTORS)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                sel.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not read %s (%s); using built-in selectors.", path, e)
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_SELECTORS, f, indent=2)
        except OSError:
            pass
    return sel


def normalize_profile_url(href: str) -> str | None:
    """Return a canonical profile URL, or None if href isn't a person's profile."""
    try:
        u = urlparse(href)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if not (host == "facebook.com" or host.endswith(".facebook.com")) or host.startswith("l."):
        return None
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        return None
    if parts[0] == "profile.php":
        ids = parse_qs(u.query).get("id")
        return f"{FB}/profile.php?id={ids[0]}" if ids and ids[0].isdigit() else None
    if parts[0] == "people" and len(parts) >= 3 and parts[2].isdigit():
        return f"{FB}/profile.php?id={parts[2]}"
    if len(parts) == 1:
        slug = parts[0]
        if slug.lower() in RESERVED_PATHS or not re.fullmatch(r"[A-Za-z0-9.\-]{3,80}", slug):
            return None
        return f"{FB}/{slug}"
    return None


@dataclass
class Friend:
    name: str
    url: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else ""

    @property
    def last_name(self) -> str:
        parts = self.name.split()
        return parts[-1] if len(parts) > 1 else ""

    @property
    def key(self) -> str:
        return self.url or self.name.lower()

    @property
    def handle(self) -> str:
        """Numeric id or username, as used in /messages/t/<handle>."""
        u = urlparse(self.url)
        if u.path.strip("/") == "profile.php":
            return (parse_qs(u.query).get("id") or [""])[0]
        return u.path.strip("/")


# JavaScript run in the page to find the "Today's birthdays" section and its links.
_EXTRACT_JS = r"""
(args) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const todayRe = new RegExp(args.today, 'i');
  const otherRe = new RegExp(args.other, 'i');
  const root = document.querySelector('[role="main"]') || document.body;
  const HSEL = 'h1,h2,h3,h4,[role="heading"],span';
  const isHeading = (el, re) => { const t = norm(el.textContent); return t.length < 60 && re.test(t); };
  const heading = Array.from(root.querySelectorAll(HSEL)).find(el => isHeading(el, todayRe));
  const otherExists = Array.from(root.querySelectorAll(HSEL)).some(el => isHeading(el, otherRe));
  if (!heading) return { found: false, otherExists, anchors: [] };
  let node = heading, container = heading.parentElement;
  while (node && node !== root) {
    node = node.parentElement;
    if (!node) break;
    if (Array.from(node.querySelectorAll(HSEL)).some(el => isHeading(el, otherRe))) break;
    container = node;
  }
  const anchors = Array.from(container.querySelectorAll('a[href]'))
    .filter(a => !a.closest('[role="navigation"],[role="banner"],[role="complementary"]'))
    .map(a => ({ href: a.href, text: norm(a.innerText || a.getAttribute('aria-label') || '') }));
  return { found: true, otherExists, anchors };
}
"""


def friends_from_anchors(anchors: list[dict], own_id: str | None = None) -> list[Friend]:
    by_url: dict[str, str] = {}
    order: list[str] = []
    for a in anchors:
        url = normalize_profile_url(a.get("href", ""))
        if not url:
            continue
        if own_id and url.endswith(f"id={own_id}"):
            continue
        if url not in by_url:
            by_url[url] = ""
            order.append(url)
        text = a.get("text", "")
        if 2 <= len(text) <= 70 and not NAME_JUNK.search(text) and len(text) > len(by_url[url]):
            by_url[url] = text
    return [Friend(name=by_url[u], url=u) for u in order if by_url[u]]


class FacebookClient:
    """Use as a context manager: `with FacebookClient(...) as fb: ...`"""

    def __init__(self, profile_dir: Path, selectors: dict, headless: bool = False,
                 channel: str = "chromium", debug_dir: Path | None = None):
        self.profile_dir = profile_dir
        self.sel = selectors
        self.headless = headless
        self.channel = channel
        self.debug_dir = debug_dir
        self._pw = None
        self.ctx = None
        self.page = None

    # --- lifecycle -------------------------------------------------------
    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise PlaywrightMissing(
                "Playwright isn't installed. Run:  pip install playwright  and then  "
                "python -m playwright install chromium") from e
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.channel and self.channel != "chromium":
            kwargs["channel"] = self.channel
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            self._pw.stop()
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise PlaywrightMissing(
                    "The browser isn't installed. Run:  python -m playwright install chromium   "
                    "(or pick Edge/Chrome under Settings → Browser).") from e
            if "ProcessSingleton" in msg or "already in use" in msg.lower():
                raise FBError("The bot's browser is already open. Close it and try again.") from e
            raise
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30_000)
        return self

    def __exit__(self, *exc):
        for closer in (lambda: self.ctx and self.ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    # --- helpers ---------------------------------------------------------
    def _pause(self, lo=0.8, hi=2.0):
        time.sleep(random.uniform(lo, hi))

    def _own_id(self) -> str | None:
        for c in self.ctx.cookies(FB):
            if c["name"] == "c_user":
                return c["value"]
        return None

    def is_logged_in(self) -> bool:
        return self._own_id() is not None

    def _goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        self._pause(1.5, 3.0)
        cur = self.page.url
        if "/login" in cur or "/checkpoint" in cur:
            raise NotLoggedIn("Facebook asked to log in again (or wants a security check). "
                              "Use “Log in to Facebook” on the Dashboard.")

    def snapshot(self, tag: str = "snapshot") -> Path | None:
        if not self.debug_dir or not self.page:
            return None
        stem = self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '_', tag.lower())[:40]}"
        try:
            self.page.screenshot(path=str(stem) + ".png", full_page=True)
            Path(str(stem) + ".html").write_text(self.page.content(), encoding="utf-8")
            return stem
        except Exception as e:
            log.debug("Snapshot failed: %s", e)
            return None

    def _insert_text(self, text: str):
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                self.page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                self.page.keyboard.press("Shift+Enter")
            time.sleep(random.uniform(0.05, 0.2))

    def _box_text(self, box) -> str:
        try:
            return box.inner_text(timeout=3_000)
        except Exception:
            return ""

    @staticmethod
    def _comparable(s: str) -> str:
        # Letters and digits only: ignores spacing, punctuation and emoji, which
        # Facebook's editor often turns into images.
        return re.sub(r"[\W_]+", "", s.lower())

    def _fill_box(self, box, text: str):
        """Clear anything Facebook pre-filled, type the message, and confirm it matches."""
        actual = ""
        for _ in range(3):
            box.click()
            self._pause(1.0, 1.8)  # give Facebook time to drop in any suggested text
            for _ in range(3):
                if not self._box_text(box).strip():
                    break
                self.page.keyboard.press("ControlOrMeta+A")
                self.page.keyboard.press("Backspace")
                self._pause(0.2, 0.5)
            self._insert_text(text)
            self._pause(0.6, 1.5)
            actual = self._box_text(box)
            if self._comparable(actual) == self._comparable(text):
                return
            log.info("  Box text didn't match the message, retrying (box had: %r)", actual[:80])
        raise SendError(f"Facebook changed the text in the box, so nothing was sent (box had: {actual[:80]!r}).")

    # --- login -----------------------------------------------------------
    def interactive_login(self, timeout_s: int = 600, stop=None) -> bool:
        """Open the login page and wait until the user has signed in (or closes the window)."""
        self.page.goto(FB if self.is_logged_in() else LOGIN_URL)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if stop is not None and stop.is_set():
                break
            if self.page.is_closed():
                break
            try:
                url = self.page.url
            except Exception:
                break
            if self.is_logged_in() and "/login" not in url and "/checkpoint" not in url:
                time.sleep(3)  # let Facebook finish writing cookies
                return True
            time.sleep(2)
        return self.is_logged_in()

    # --- birthdays -------------------------------------------------------
    def todays_birthdays(self) -> list[Friend]:
        self._goto(BIRTHDAYS_URL)
        result = self.page.evaluate(_EXTRACT_JS, {"today": self.sel["today_heading_regex"],
                                                  "other": self.sel["other_heading_regex"]})
        if not result["found"]:
            if result["otherExists"]:
                return []  # page loaded fine, just nobody today
            shot = self.snapshot("birthdays_page_unrecognized")
            raise FBError("Couldn't recognise the birthdays page layout. "
                          f"A screenshot was saved{' to ' + str(shot) if shot else ''}. "
                          "If your Facebook isn't in English, edit selectors.json.")
        return friends_from_anchors(result["anchors"], self._own_id())

    # --- sending ---------------------------------------------------------
    def send(self, friend: Friend, text: str, mode: str = "messenger", fallback: bool = True) -> str:
        """Send the message; returns the method actually used."""
        if mode == "timeline":
            try:
                self.post_on_timeline(friend, text)
                return "timeline"
            except SendError as e:
                if not fallback:
                    raise
                log.info("  Timeline post didn't work for %s (%s) – trying Messenger.", friend.name, e)
        self.send_messenger(friend, text)
        return "messenger"

    def _check_pin_prompt(self):
        try:
            body = self.page.inner_text("body", timeout=3_000)
        except Exception:
            return
        if re.search(self.sel["pin_prompt_regex"], body, re.I):
            raise SendError("Messenger is asking for your chat PIN. Click “Log in to Facebook”, "
                            "open Messenger in that window and enter the PIN once.")

    def _open_chat_box(self, friend: Friend):
        page = self.page
        self._goto(friend.url)
        btn = None
        for name in self.sel["message_button_names"]:
            loc = page.get_by_role("button", name=name, exact=True)
            if loc.count():
                btn = loc.first
                break
        if btn is None:
            loc = page.locator(self.sel["message_button_css"])
            if loc.count():
                btn = loc.first
        if btn is not None:
            btn.click()
        else:
            # Some profiles hide the button; go straight to the conversation instead.
            self._goto(f"{FB}/messages/t/{friend.handle}")
        box = page.locator(self.sel["chat_textbox"]).last
        try:
            box.wait_for(state="visible", timeout=20_000)
        except Exception:
            self._check_pin_prompt()
            raise SendError("Couldn't find the chat message box.")
        return box

    def send_messenger(self, friend: Friend, text: str):
        box = self._open_chat_box(friend)
        self._check_pin_prompt()
        self._fill_box(box, text)
        self.page.keyboard.press("Enter")
        self._pause(2.0, 3.5)
        try:
            leftover = box.inner_text(timeout=3_000).strip()
        except Exception:
            leftover = ""
        if leftover:
            raise SendError("The message is still sitting in the box; it may not have sent.")
        try:
            close = self.page.locator(self.sel["close_chat"]).first
            if close.count() and close.is_visible():
                close.click()
        except Exception:
            pass

    def post_on_timeline(self, friend: Friend, text: str):
        if "/events/birthdays" not in self.page.url:
            self._goto(BIRTHDAYS_URL)
        handle = friend.handle
        token = f"id={handle}" if handle.isdigit() else f"/{handle}"
        link = self.page.locator(f"a[href*='{token}']").first
        if not link.count():
            raise SendError("Friend's card not found on the birthdays page.")
        box = None
        for depth in range(1, 14):
            anc = link.locator(f"xpath=ancestor::*[{depth}]")
            boxes = anc.locator(self.sel["timeline_textbox"])
            n = boxes.count()
            if n == 1:
                box = boxes.first
                break
            if n > 1:
                break  # climbed into a container holding several cards
        if box is None:
            raise SendError("No 'write on timeline' box for this friend.")
        self._fill_box(box, text)
        self.page.keyboard.press("Enter")
        self._pause(2.5, 4.0)
        try:
            if box.inner_text(timeout=3_000).strip():
                raise SendError("The post is still in the box; it may not have been published.")
        except SendError:
            raise
        except Exception:
            pass

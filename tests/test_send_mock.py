"""End-to-end test against a fake Facebook served through Playwright request routing.

Needs a Playwright browser (`python -m playwright install chromium`); skipped otherwise.
The fake text boxes pre-fill "Happy Birthday, <name>!" when clicked, like Facebook can,
to make sure the bot clears them before typing.
"""
import datetime
import json
import re

import pytest

pytest.importorskip("playwright")

from birthday_bot import fb_client
from birthday_bot import settings as S
from birthday_bot.runner import BirthdayRunner

PREFILL_JS = """
<script>
document.addEventListener('focusin', e => {
  const b = e.target;
  if (b.getAttribute && b.getAttribute('contenteditable') === 'true' && !b.dataset.filled) {
    b.dataset.filled = '1';
    b.innerText = 'Happy Birthday, ' + (b.dataset.who || 'friend') + '!';
  }
});
document.addEventListener('keydown', e => {
  const b = document.activeElement;
  if (!b || b.getAttribute('contenteditable') !== 'true') return;
  if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); document.execCommand('insertLineBreak'); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    fetch('/__sent', {method: 'POST', body: JSON.stringify({to: b.getAttribute('aria-label'), text: b.innerText})});
    b.innerHTML = '';
  }
});
</script>
"""

BIRTHDAYS = """<html><body>
<div role="navigation"><a href="https://www.facebook.com/profile.php?id=999">Me Myself</a></div>
<div role="main">
 <div><h2><span>Today's birthdays</span></h2>
  <div><a href="https://www.facebook.com/alice.smith?__tn__=x"><img></a>
       <a href="https://www.facebook.com/alice.smith?ref=bday">Alice Smith</a><span>Turning 30</span>
       <div role="textbox" contenteditable="true" data-who="Alice" aria-label="Write on Alice's timeline"></div></div>
  <div><a href="https://www.facebook.com/profile.php?id=12345&amp;x=1">Bob Jones</a>
       <div role="textbox" contenteditable="true" data-who="Bob" aria-label="Write on Bob's timeline"></div></div>
  <div><a href="https://www.facebook.com/carol.w">Carol White</a></div>
 </div>
 <div><h2><span>Recent birthdays</span></h2><a href="https://www.facebook.com/dave.old">Dave Old</a></div>
</div>""" + PREFILL_JS + "</body></html>"

PROFILE = """<html><body><div role="main"><h1>{who}</h1>
<div role="button" aria-label="Message" onclick="document.getElementById('chat').style.display='block'">Message</div>
<div id="chat" style="display:none">
  <div role="textbox" contenteditable="true" data-who="{who}" aria-label="Message {who}"></div>
  <div aria-label="Close chat">x</div></div></div>""" + PREFILL_JS + "</body></html>"


@pytest.fixture
def fake_facebook(monkeypatch):
    sent = []
    original_enter = fb_client.FacebookClient.__enter__

    def enter(self):
        try:
            client = original_enter(self)
        except fb_client.PlaywrightMissing as e:
            pytest.skip(str(e))
        self.ctx.add_cookies([{"name": "c_user", "value": "999", "domain": ".facebook.com", "path": "/"}])

        def handle(route):
            url = route.request.url
            if "/__sent" in url:
                sent.append(json.loads(route.request.post_data))
                return route.fulfill(status=200, body="ok")
            if "/events/birthdays" in url:
                return route.fulfill(status=200, content_type="text/html", body=BIRTHDAYS)
            m = re.search(r"facebook\.com/(?:profile\.php\?id=(\d+)|([\w.]+))", url)
            who = {"alice.smith": "Alice", "12345": "Bob"}.get((m.group(1) or m.group(2)) if m else "", "Someone")
            return route.fulfill(status=200, content_type="text/html", body=PROFILE.replace("{who}", who))

        self.ctx.route("**/*", handle)
        return client

    monkeypatch.setattr(fb_client.FacebookClient, "__enter__", enter)
    monkeypatch.setattr(fb_client.FacebookClient, "_pause", lambda self, lo=0, hi=0: None)
    return sent


def make_runner(mode):
    st = S.Settings()
    for k, v in {"headless": True, "delay_min_sec": 0, "delay_max_sec": 0, "send_mode": mode,
                 "templates": ["Happy birthday, {first_name}! 🎉\nHave a great one"],
                 "custom_messages": {"Bob Jones": "Yo {first_name}, HBD!"},
                 "excluded": ["Carol White"]}.items():
        st.set(k, v)
    return BirthdayRunner(st, S.State())


@pytest.mark.parametrize("mode", ["messenger", "timeline"])
def test_sends_exactly_the_template(fake_facebook, mode):
    runner = make_runner(mode)

    preview = runner.run(dry_run=True)
    assert preview.found == 3 and preview.previewed == 2 and fake_facebook == []

    result = runner.run(dry_run=False)
    assert result.sent == 2, result.message
    texts = [m["text"] for m in fake_facebook]
    # No pre-filled "Happy Birthday, Alice!" in front of the message
    assert texts[0] == "Happy birthday, Alice! 🎉\nHave a great one"
    assert texts[1] == "Yo Bob, HBD!"

    again = runner.run(dry_run=False)
    assert again.sent == 0 and len(fake_facebook) == 2  # nobody is messaged twice a day

    today = S.State().day_entries(datetime.date.today().isoformat())
    assert {e["status"] for e in today.values()} == {"sent", "excluded"}

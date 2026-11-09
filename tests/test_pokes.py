"""Auto-poker tests: decision rules (no browser) and a run against a fake pokes page."""
import json

import pytest

from birthday_bot.fb_client import Friend, PokeEntry
from birthday_bot.runner import poke_decision, poke_rule_key

ALICE = Friend("Alice Smith", "https://www.facebook.com/alice.smith")
DAN = Friend("Dan Miller", "https://www.facebook.com/profile.php?id=4242")


def entry(friend, text=""):
    return PokeEntry(index=0, friend=friend, text=text or (friend.name + " poked you") if friend else text)


def test_rule_keys():
    assert poke_rule_key("https://www.facebook.com/alice.smith?ref=x") == "https://www.facebook.com/alice.smith"
    assert poke_rule_key("  Dan Miller ") == "dan miller"


def test_decisions():
    cfg = {"poke_mode": "everyone", "poke_rules": {DAN.url: "ignore"}}
    assert poke_decision(cfg, entry(ALICE)) == "poke"
    assert poke_decision(cfg, entry(DAN)) == "ignore"
    cfg = {"poke_mode": "selected", "poke_rules": {ALICE.url: "auto"}}
    assert poke_decision(cfg, entry(ALICE)) == "poke"
    assert poke_decision(cfg, entry(DAN)) == "leave"
    assert poke_decision(cfg, PokeEntry(0, None, "Someone poked you")) == "unknown"


def test_name_ignore_also_matches_card_text():
    # Even if the link was read as someone else, an ignored name in the card wins.
    cfg = {"poke_mode": "everyone", "poke_rules": {"dan miller": "ignore"}}
    wrong = Friend("Someone Else", "https://www.facebook.com/someone")
    assert poke_decision(cfg, PokeEntry(0, wrong, "Dan Miller poked you 3 times")) == "ignore"


# ---------------------------------------------------------------------------
pytest.importorskip("playwright")

from birthday_bot import fb_client  # noqa: E402
from birthday_bot import settings as S  # noqa: E402
from birthday_bot.runner import BirthdayRunner  # noqa: E402

CARD = """<div class="card" id="c-{slug}"><a href="https://www.facebook.com/{slug}"><img></a>
  <div><a href="https://www.facebook.com/{slug}">{name}</a> poked you.</div>
  <div role="button" tabindex="0" onclick="pokeBack('{slug}', '{name}')"><span>Poke back</span></div></div>"""

POKES_PAGE = """<html><body>
<div role="navigation"><a href="https://www.facebook.com/profile.php?id=999">Me Myself</a></div>
<div role="main"><h2>Pokes</h2><div id="list">{cards}</div>
 <h2>Suggested pokes</h2>
 <div><a href="https://www.facebook.com/carol.w">Carol White</a><div role="button" onclick="fetch('/__poked',{{method:'POST',body:JSON.stringify({{to:'carol.w',kind:'new'}})}})">Poke</div></div>
</div>
<script>
function pokeBack(slug, name) {{
  fetch('/__poked', {{method: 'POST', body: JSON.stringify({{to: slug, kind: 'back'}})}});
  // Facebook re-renders the list after a poke; rebuild every card from scratch.
  const list = document.getElementById('list');
  const html = list.innerHTML;
  list.innerHTML = '';
  list.innerHTML = html;
  document.getElementById('c-' + slug).innerHTML = 'You poked ' + name + '.';
}}
</script></body></html>"""


def page_with(people):
    return POKES_PAGE.format(cards="".join(CARD.format(slug=s, name=n) for s, n in people))


@pytest.fixture
def fake_pokes(monkeypatch):
    poked, page = [], {"html": ""}
    original_enter = fb_client.FacebookClient.__enter__

    def enter(self):
        try:
            client = original_enter(self)
        except fb_client.PlaywrightMissing as e:
            pytest.skip(str(e))
        self.ctx.add_cookies([{"name": "c_user", "value": "999", "domain": ".facebook.com", "path": "/"}])

        def handle(route):
            if "/__poked" in route.request.url:
                poked.append(json.loads(route.request.post_data))
                return route.fulfill(status=200, body="ok")
            return route.fulfill(status=200, content_type="text/html", body=page["html"])

        self.ctx.route("**/*", handle)
        return client

    monkeypatch.setattr(fb_client.FacebookClient, "__enter__", enter)
    monkeypatch.setattr(fb_client.FacebookClient, "_pause", lambda self, lo=0, hi=0: None)
    monkeypatch.setattr("birthday_bot.runner.random.uniform", lambda a, b: 0)
    return poked, page


def runner_with(mode, rules):
    st = S.Settings()
    st.set("poke_mode", mode)
    st.set("poke_rules", rules)
    return BirthdayRunner(st, S.State())


PEOPLE = [("alice.smith", "Alice Smith"), ("dan.miller", "Dan Miller"), ("bob.j", "Bob Jones")]


def test_everyone_mode_skips_ignored_friend(fake_pokes):
    poked, page = fake_pokes
    page["html"] = page_with(PEOPLE)
    r = runner_with("everyone", {"https://www.facebook.com/dan.miller": "ignore"})

    scan = r.poke(scan_only=True)
    assert scan.waiting == 3 and poked == []
    assert set(S.State().pokers()) == {f"https://www.facebook.com/{s}" for s, _ in PEOPLE}

    res = r.poke()
    assert res.poked == 2 and res.ignored == 1, res.message
    assert sorted(p["to"] for p in poked) == ["alice.smith", "bob.j"]
    assert all(p["kind"] == "back" for p in poked)  # suggested pokes never clicked


def test_selected_mode_only_pokes_picked(fake_pokes):
    poked, page = fake_pokes
    page["html"] = page_with(PEOPLE)
    res = runner_with("selected", {"https://www.facebook.com/bob.j": "auto"}).poke()
    assert res.poked == 1 and res.left == 2
    assert [p["to"] for p in poked] == ["bob.j"]


def test_lone_ignored_card_is_left_alone(fake_pokes):
    # With only one card on the page, make sure it isn't confused with Suggested pokes.
    poked, page = fake_pokes
    page["html"] = page_with([("dan.miller", "Dan Miller")])
    res = runner_with("everyone", {"https://www.facebook.com/dan.miller": "ignore"}).poke()
    assert res.waiting == 1 and res.ignored == 1 and poked == []
    assert [p["name"] for p in S.State().pokers().values()] == ["Dan Miller"]

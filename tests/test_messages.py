from birthday_bot.fb_client import Friend, friends_from_anchors, normalize_profile_url
from birthday_bot.runner import choose_message, is_excluded, render_message

ALICE = Friend("Alice Smith", "https://www.facebook.com/alice.smith")


def test_placeholders():
    assert render_message("Hi {first_name} {last_name} / {name}", ALICE) == "Hi Alice Smith / Alice Smith"


def test_custom_message_wins():
    cfg = {"templates": ["generic"], "custom_messages": {"alice smith": "Yo {first_name}"}}
    assert choose_message(cfg, ALICE) == "Yo Alice"


def test_exclusions():
    assert is_excluded({"excluded": ["Alice Smith"]}, ALICE)
    assert is_excluded({"excluded": ["alice.smith"]}, ALICE)
    assert not is_excluded({"excluded": ["Bob"]}, ALICE)


def test_profile_urls():
    n = normalize_profile_url
    assert n("https://www.facebook.com/alice.smith?ref=bday") == "https://www.facebook.com/alice.smith"
    assert n("https://www.facebook.com/profile.php?id=123&x=1") == "https://www.facebook.com/profile.php?id=123"
    assert n("https://m.facebook.com/people/Jane-Doe/100012/") == "https://www.facebook.com/profile.php?id=100012"
    assert n("https://www.facebook.com/events/birthdays/") is None
    assert n("https://l.facebook.com/l.php?u=x") is None
    assert n("https://example.com/alice") is None


def test_friends_from_anchors_skips_own_profile_and_junk():
    anchors = [
        {"href": "https://www.facebook.com/alice.smith", "text": ""},
        {"href": "https://www.facebook.com/alice.smith?x", "text": "Alice Smith"},
        {"href": "https://www.facebook.com/profile.php?id=999", "text": "Me Myself"},
        {"href": "https://www.facebook.com/bob.j", "text": "Turning 30"},
    ]
    friends = friends_from_anchors(anchors, own_id="999")
    assert [f.name for f in friends] == ["Alice Smith"]

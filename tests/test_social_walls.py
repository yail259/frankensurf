"""Bot checks and login forms that came back looking like pages (synthetic text only)."""
from frankensurf.runtime import _challenge_text, _login_wall, _wall_after_parse


def test_challenge_titles_match_inside_a_site_name():
    assert _challenge_text("Example - Prove your humanity", "Prove your humanity. But not for bots.")
    assert _challenge_text("Prove your humanity", "")
    assert _challenge_text("One more step | Example", "")
    assert not _challenge_text("Proving theorems - Example", "word " * 1000)


def test_a_login_form_standing_in_for_a_deep_link_is_a_wall():
    form = "Example Explore the things you love. Log into Example Email or mobile number Password Log in Forgot account?"
    assert _wall_after_parse("Example", form, "https://www.example.com/SomePage/", None) == "AUTH_REQUIRED"
    assert _wall_after_parse("Log into Example", "anything", "https://www.example.com/p/1", None) == "AUTH_REQUIRED"


def test_real_pages_with_a_login_link_are_not_walls():
    profile = "Some Person (@person) / Example Log in Sign up Some Person 1,715 posts Joined 2021 " + "post text " * 100
    assert _wall_after_parse("Some Person (@person) / Example", profile, "https://example.com/person", None) is None
    # The homepage of a site titled with its own name is the page asked for.
    assert not _login_wall("Example", "Log in Password", "https://www.example.com/")
    # A long page that mentions a password is an article, not a form.
    assert not _login_wall("Example", "log in password " * 1000, "https://www.example.com/help/passwords")
    # Asking for the login page itself is fine.
    assert _wall_after_parse("Log in", "Password", "https://www.example.com/login", None) is None

import httpx

from frankensurf.runtime import Runtime, _wall_after_parse

URL = "https://shop.example.com/search?q=laptop"


def test_named_interstitials_are_walls():
    assert _wall_after_parse("Robot or human?", "Activate and hold the button", URL, URL) == "CAPTCHA"
    assert _wall_after_parse("# Just a moment...", "We must verify your session", URL, URL) == "CAPTCHA"
    assert _wall_after_parse("Pardon Our Interruption", "x", URL, URL) == "CAPTCHA"
    long_article = "robot or human " + "words " * 1000
    assert _wall_after_parse("An essay", long_article, URL, URL) is None


def test_redirect_to_sign_in_is_auth_required():
    assert _wall_after_parse("Sign in", "Email", URL, "https://signin.example.com/ws/eBayISAPI.dll?SignIn") == "AUTH_REQUIRED"
    assert _wall_after_parse("Sign in", "Email", URL, "https://shop.example.com/login?next=/search") == "AUTH_REQUIRED"
    assert _wall_after_parse("Login help", "text", "https://shop.example.com/login", "https://shop.example.com/login") is None
    assert _wall_after_parse("Results", "text", URL, "https://shop.example.com/search?q=laptop&page=1") is None


async def test_challenge_served_as_200_is_not_observed(tmp_path):
    page = "<html><head><title>Robot or human?</title></head><body>Activate and hold the button to confirm that you are human.</body></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.read(URL, policy_overrides={"provider": "http"})
    assert result["receipt"]["failure"]["code"] == "CAPTCHA"

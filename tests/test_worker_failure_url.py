from frankensurf import provider_worker


def test_failed_navigation_keeps_real_code_and_drops_browser_internal_url():
    for url in ("about:blank", "chrome-error://chromewebdata/"):
        packet = provider_worker._failed("TIMEOUT", None, url)
        assert packet == {"status": "failed", "failure": "TIMEOUT"}


def test_failed_navigation_to_credential_url_is_still_invalid():
    packet = provider_worker._failed("TIMEOUT", None, "https://user:pw@example.com/")
    assert packet == {"status": "failed", "failure": "INVALID_URL"}


def test_failed_navigation_keeps_valid_response_url():
    url = "https://www.gumtree.com.au/s-sydney/macbook+pro+m4/k0l3003435?sort=date"
    packet = provider_worker._failed("BLOCKED", 403, url)
    assert packet == {"status": "failed", "failure": "BLOCKED", "http_status": 403, "url": url}


def test_result_on_blank_page_is_transient_not_invalid_url():
    packet = provider_worker._result("about:blank", "", "text/html", 200)
    assert packet == {"status": "failed", "failure": "PROVIDER_DOWN"}

"""Standalone pinned Crawl4AI worker.

The worker accepts one closed-schema anonymous public read/extract request. It
never accepts cookies, headers, profiles, proxy configuration, browser arguments,
JavaScript, LLM configuration or credentials from the caller.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import math
import os
from pathlib import Path
import sys
from urllib.parse import urlparse


SDK_VERSION = "0.9.4"
WORKER_SCHEMA = "frankensurf.crawl4ai-worker/v1"
HEALTH_SCHEMA = "frankensurf.crawl4ai-health/v1"
FAILURES = frozenset({
    "AUTH_REQUIRED", "BLOCKED", "CAPTCHA", "LIMIT_EXCEEDED", "NOT_FOUND",
    "POLICY_DENIED", "PROVIDER_DOWN", "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED", "TIMEOUT",
})
# A JSON string can expand one input byte to six bytes (for example, a
# control character encoded as \u00XX). The fixed envelope covers the closed
# schema, two Core-bounded selectors and protocol metadata. This is derived from
# Core's content byte budget; it is not a provider option or authority ceiling.
_PACKET_ENCODING_EXPANSION = 6
_PACKET_ENVELOPE_MAX_BYTES = 64 * 1024
_READINESS_POLL_MS = 100
_ENABLE_STEALTH = True


def _packet_max_bytes(content_max_bytes):
    if type(content_max_bytes) is not int or content_max_bytes < 1:
        raise ValueError("content byte budget must be a positive integer")
    return (
        content_max_bytes * _PACKET_ENCODING_EXPANSION
        + _PACKET_ENVELOPE_MAX_BYTES
    )


UNSAFE_BROWSER_ARGUMENTS = (
    "--no-sandbox",
    "--ignore-certificate-errors",
    "--disable-web-security",
    "--disable-client-side-phishing-detection",
    "--disable-ipc-flooding-protection",
    "--safebrowsing-disable-auto-update",
    "--disable-cookie-encryption",
    "--password-store=",
    "--use-mock-keychain",
)


def _valid_url(value):
    try:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        parsed.port
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def _selector(value):
    return (value is None or isinstance(value, str) and value.strip()
            and all(ord(character) >= 32 and ord(character) != 127
                    for character in value))


def _number(value, *, positive=True):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _validated_request(value):
    keys = {
        "schema", "provider_version", "url", "operation", "timeout_seconds",
        "max_bytes", "settle_ms", "headless", "wait_selector",
        "wait_state", "content_ready_selector",
        "content_ready_timeout_seconds",
    }
    if (type(value) is not dict or set(value) != keys
            or value.get("schema") != WORKER_SCHEMA
            or value.get("provider_version") != SDK_VERSION
            or not _valid_url(value.get("url"))
            or value.get("operation") not in {"read", "extract"}
            or not _number(value.get("timeout_seconds"))
            or type(value.get("max_bytes")) is not int
            or value["max_bytes"] < 1
            or type(value.get("settle_ms")) is not int
            or value["settle_ms"] < 0
            or type(value.get("headless")) is not bool
            or not _selector(value.get("wait_selector"))
            or value.get("wait_state") not in {"attached", "visible"}
            or not _selector(value.get("content_ready_selector"))
            or not _number(value.get("content_ready_timeout_seconds"))):
        raise ValueError()
    return value


def _status_failure(status):
    return {
        401: "AUTH_REQUIRED",
        403: "BLOCKED",
        404: "NOT_FOUND",
        410: "NOT_FOUND",
        429: "RATE_LIMITED",
    }.get(status) or ("PROVIDER_DOWN" if isinstance(status, int)
                      and status >= 500 else None)


def _failure(code, *, url=None, status=None, content=None):
    packet = {
        "schema": WORKER_SCHEMA,
        "provider_version": SDK_VERSION,
        "status": "failed",
        "failure": code if code in FAILURES else "PROVIDER_DOWN",
        "cost_usd": 0,
    }
    if _valid_url(url):
        packet["url"] = url
    if type(status) is int and 100 <= status <= 599:
        packet["http_status"] = status
    if isinstance(content, str):
        packet["content"] = content
        packet["content_type"] = "text/html; rendered=1"
    return packet


def _safe_browser_patch():
    """Restore Chromium/TLS protections disabled by Crawl4AI defaults."""
    from crawl4ai.browser_manager import BrowserManager
    original = BrowserManager._build_browser_args
    if getattr(original, "_frankensurf_safe", False):
        return

    def safe_arguments(manager):
        result = original(manager)
        if type(result) is not dict or type(result.get("args")) is not list:
            raise RuntimeError("unsupported Crawl4AI browser arguments")
        result = dict(result)
        result["args"] = [
            argument for argument in result["args"]
            if not any(argument == prefix or argument.startswith(prefix)
                       for prefix in UNSAFE_BROWSER_ARGUMENTS)
        ]
        if any(any(argument == prefix or argument.startswith(prefix)
                   for prefix in UNSAFE_BROWSER_ARGUMENTS)
               for argument in result["args"]):
            raise RuntimeError("unsafe Crawl4AI browser argument")
        return result

    safe_arguments._frankensurf_safe = True
    BrowserManager._build_browser_args = safe_arguments


def _sdk():
    import crawl4ai.__version__ as release
    if release.__version__ != SDK_VERSION:
        raise RuntimeError("Crawl4AI version mismatch")
    _safe_browser_patch()
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    return AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig


def _browser_config(BrowserConfig, request):
    return BrowserConfig(
        browser_type="chromium",
        browser_mode="dedicated",
        headless=request["headless"],
        use_managed_browser=False,
        use_persistent_context=False,
        user_data_dir=None,
        cdp_url=None,
        proxy=None,
        proxy_config=None,
        accept_downloads=False,
        storage_state=None,
        ignore_https_errors=False,
        java_script_enabled=True,
        verbose=False,
        cookies=[],
        headers={},
        enable_stealth=_ENABLE_STEALTH,
        extra_args=[],
        init_scripts=[],
    )


def _readiness_script(request):
    selector = request["content_ready_selector"]
    if selector is None:
        return None
    timeout_ms = min(
        int(request["content_ready_timeout_seconds"] * 1000),
        int(request["timeout_seconds"] * 1000),
    )
    poll_ms = _READINESS_POLL_MS
    selector_json = json.dumps(selector)
    visible = (
        "if(!element)return false;"
        "const style=getComputedStyle(element);"
        "return style.visibility!=='hidden'&&style.display!=='none'"
        "&&element.getClientRects().length>0;"
        if request["wait_state"] == "visible" else
        "return Boolean(element);"
    )
    return (
        "const selector=" + selector_json + ";"
        "const deadline=Date.now()+" + str(timeout_ms) + ";"
        "const ready=()=>{const element=document.querySelector(selector);"
        + visible + "};"
        "while(true){if(ready())return {status:'satisfied'};"
        "if(Date.now()>=deadline)return {status:'timed_out'};"
        "await new Promise(resolve=>setTimeout(resolve,"
        + str(poll_ms) + "));}"
    )


def _required_wait(request):
    selector = request["wait_selector"]
    if selector is None:
        return None
    selector_json = json.dumps(selector)
    condition = (
        "const element=document.querySelector(" + selector_json + ");"
        "if(!element)return false;"
        "const style=getComputedStyle(element);"
        "return style.visibility!=='hidden'&&style.display!=='none'"
        "&&element.getClientRects().length>0;"
        if request["wait_state"] == "visible" else
        "return Boolean(document.querySelector(" + selector_json + "));"
    )
    return "js:() => {" + condition + "}"


def _readiness_result(value, request):
    if request["content_ready_selector"] is None:
        return None
    try:
        item = value["results"][0]
        status = item["status"]
    except (KeyError, IndexError, TypeError):
        status = "timed_out"
    if status not in {"satisfied", "timed_out"}:
        status = "timed_out"
    return {
        "status": status,
        "timeout_seconds": request["content_ready_timeout_seconds"],
    }


async def _acquire(request):
    AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig = _sdk()
    browser = _browser_config(BrowserConfig, request)
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",
        page_timeout=max(1, int(request["timeout_seconds"] * 1000)),
        wait_for=_required_wait(request),
        wait_for_timeout=max(1, int(request["timeout_seconds"] * 1000)),
        delay_before_return_html=request["settle_ms"] / 1000,
        js_code=_readiness_script(request),
        max_retries=0,
        check_robots_txt=False,
        process_in_browser=False,
        capture_network_requests=False,
        capture_console_messages=False,
        screenshot=False,
        pdf=False,
        verbose=False,
        log_console=False,
    )
    base = os.environ.get("CRAWL4AI_BASE_DIRECTORY", str(Path.cwd()))
    try:
        async with AsyncWebCrawler(config=browser, base_directory=base) as crawler:
            result = await crawler.arun(url=request["url"], config=config)
    except TimeoutError:
        return _failure("TIMEOUT", url=request["url"])
    except Exception as error:
        detail = str(error).lower()
        code = "TIMEOUT" if "timeout" in detail else "PROVIDER_DOWN"
        return _failure(code, url=request["url"])

    final_url = result.redirected_url or result.url or request["url"]
    status = result.status_code
    if not _valid_url(final_url):
        return _failure("PROVIDER_DOWN")
    if type(status) is not int or not 100 <= status <= 599:
        return _failure("PROVIDER_DOWN", url=final_url)
    content = result.html
    bounded_failure_content = (
        content if isinstance(content, str)
        and len(content.encode()) <= request["max_bytes"] else None
    )
    if failure := _status_failure(status):
        return _failure(
            failure, url=final_url, status=status,
            content=bounded_failure_content,
        )
    if result.success is not True:
        detail = str(result.error_message or "").lower()
        code = "TIMEOUT" if "timeout" in detail else "PROVIDER_DOWN"
        return _failure(
            code, url=final_url, status=status,
            content=bounded_failure_content,
        )
    if not isinstance(content, str):
        return _failure("PROVIDER_DOWN", url=final_url, status=status)
    if len(content.encode()) > request["max_bytes"]:
        return _failure("LIMIT_EXCEEDED", url=final_url, status=status)
    packet = {
        "schema": WORKER_SCHEMA,
        "provider_version": SDK_VERSION,
        "status": "ok",
        "url": final_url,
        "content": content,
        "content_type": "text/html; rendered=1",
        "http_status": status,
        "cost_usd": 0,
    }
    readiness = _readiness_result(result.js_execution_result, request)
    if readiness is not None:
        packet["content_readiness"] = readiness
    return packet


async def _health():
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    patchright = os.environ.get("PATCHRIGHT_BROWSERS_PATH")
    if (not browsers or not patchright or browsers != patchright
            or not Path(browsers).is_absolute()):
        raise RuntimeError("isolated browser path is required")
    AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig = _sdk()
    request = {"headless": True}
    browser = _browser_config(BrowserConfig, request)
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=10000,
        process_in_browser=True,
        delay_before_return_html=0,
        max_retries=0,
        check_robots_txt=False,
        verbose=False,
        log_console=False,
    )
    source = (
        "raw:<html><body><div id='state'>pending</div>"
        "<script>document.getElementById('state').textContent='health-ok';</script>"
        "</body></html>"
    )
    base = os.environ.get("CRAWL4AI_BASE_DIRECTORY", str(Path.cwd()))
    async with AsyncWebCrawler(config=browser, base_directory=base) as crawler:
        result = await crawler.arun(url=source, config=config)
        executable = Path(
            crawler.crawler_strategy.browser_manager.playwright.chromium.executable_path
        ).resolve(strict=True)
        executable.relative_to(Path(browsers).resolve(strict=True))
    if result.success is not True or "health-ok" not in result.html:
        raise RuntimeError("Crawl4AI health render failed")
    return {
        "schema": HEALTH_SCHEMA,
        "status": "ok",
        "sdk_version": SDK_VERSION,
        "browser_executable": str(executable),
    }


def _emit(packet, maximum=None):
    raw = json.dumps(
        packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if maximum is not None and len(raw) > maximum:
        raw = json.dumps(
            _failure("LIMIT_EXCEEDED"),
            sort_keys=True, separators=(",", ":"),
        ).encode()
    sys.__stdout__.buffer.write(raw)
    sys.__stdout__.buffer.flush()


def main():
    if sys.argv[1:] == ["--health"]:
        try:
            with redirect_stdout(sys.stderr):
                packet = asyncio.run(_health())
        except Exception:
            packet = _failure("PROVIDER_UNAVAILABLE")
        _emit(packet, 16 * 1024)
        return 0 if packet.get("status") == "ok" else 1
    try:
        if (sys.argv[1:2] != ["--request-max-bytes"]
                or len(sys.argv) != 3):
            raise ValueError()
        request_maximum = int(sys.argv[2])
        if request_maximum < 1024:
            raise ValueError()
    except ValueError:
        _emit(_failure("POLICY_DENIED"), 16 * 1024)
        return 1
    try:
        raw = sys.stdin.buffer.read(request_maximum + 1)
        if len(raw) > request_maximum:
            raise ValueError()
        request = _validated_request(json.loads(raw))
        if _packet_max_bytes(request["max_bytes"]) != request_maximum:
            raise ValueError()
    except (ValueError, UnicodeError):
        _emit(_failure("POLICY_DENIED"), 16 * 1024)
        return 1
    try:
        with redirect_stdout(sys.stderr):
            packet = asyncio.run(_acquire(request))
    except Exception:
        packet = _failure("PROVIDER_DOWN", url=request["url"])
    _emit(packet, request_maximum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

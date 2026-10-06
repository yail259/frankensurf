"""FrankenSurf profiles: a login that any capable provider can carry.

A profile is the BrowserProfileStore of SPEC section 12, owned by FrankenSurf
rather than by one browser: a named, encrypted, versioned snapshot of a signed-in
session (Playwright storage state: cookies and local storage), the sites it
covers, a pinned fingerprint and a sharing level.

- ``frankensurf profile-login NAME --site example.com`` opens a visible
  FrankenSurf Chromium. You sign in; when you close the window the session is
  captured, encrypted and stored. Core never types or clicks here.
- A read with ``profile=NAME`` runs on any provider that can carry a session and
  that the profile's sharing level allows: ``local`` (plain HTTP with the
  cookies, FrankenSurf's Chromium, Patchright) or ``hosted`` (adds the hosted
  Chromium browsers). Camoufox is Firefox, so it would contradict the pinned
  Chromium fingerprint and is left out.
- Cookies are sent only to the profile's sites, and a profile read of any other
  site is refused. Each read's refreshed cookies are saved back.
- Profiles live in ``~/.local/share/frankensurf/profiles`` as AES-GCM files. The
  key is ``FRANKENSURF_PROFILE_KEY`` (base64, 32 bytes; keep it in a secrets
  manager such as Infisical) or a 0600 key file created on first use at
  ``~/.config/frankensurf/profile.key``. Agents see profile names, never the
  session.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

STORE = Path("~/.local/share/frankensurf/profiles")
KEY_FILE = Path("~/.config/frankensurf/profile.key")
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SHARING = ("local", "hosted")
# Providers that can carry a session, by sharing level. Order follows routing.
LOCAL_CARRIERS = ("http", "local", "patchright")
HOSTED_CARRIERS = ("steel", "steel_cloud", "browserbase", "hyperbrowser", "browserless",
                   "kernel", "anchor")


class ProfileError(ValueError):
    pass


def _key() -> bytes:
    from .hosted_providers import _setting
    configured = _setting("profile_key")
    if configured:
        key = base64.b64decode(configured)
    else:
        path = KEY_FILE.expanduser()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(base64.b64encode(secrets.token_bytes(32)).decode())
        key = base64.b64decode(path.read_text().strip())
    if len(key) != 32:
        raise ProfileError("Profile key must be 32 bytes, base64-encoded")
    return key


def _seal(data: dict, name: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_key()).encrypt(nonce, json.dumps(data).encode(), name.encode())
    return b"FSP1" + nonce + sealed


def _open(blob: bytes, name: str) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(b"FSP1"):
        raise ProfileError("Not a FrankenSurf profile file")
    try:
        return json.loads(AESGCM(_key()).decrypt(blob[4:16], blob[16:], name.encode()))
    except InvalidTag:
        raise ProfileError("Profile cannot be decrypted with this key") from None


def site_matches(host: str, sites) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return any(host == site or host.endswith("." + site) for site in sites)


@dataclass
class Profile:
    name: str
    sites: list[str]
    sharing: str = "local"
    fingerprint: dict = field(default_factory=dict)
    state: dict = field(default_factory=lambda: {"cookies": [], "origins": []})
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def carriers(self) -> tuple[str, ...]:
        return LOCAL_CARRIERS + (HOSTED_CARRIERS if self.sharing == "hosted" else ())

    def covers(self, url: str) -> bool:
        return site_matches(urlparse(url).hostname or "", self.sites)

    def scoped_state(self) -> dict:
        """Only the cookies and storage for this profile's sites."""
        cookies = [c for c in self.state.get("cookies", [])
                   if site_matches(str(c.get("domain", "")).lstrip("."), self.sites)]
        origins = [o for o in self.state.get("origins", [])
                   if site_matches(urlparse(o.get("origin", "")).hostname or "", self.sites)]
        return {"cookies": cookies, "origins": origins}

    def cookie_header(self, url: str) -> str | None:
        parsed = urlparse(url)
        host, path, now = (parsed.hostname or "").lower(), parsed.path or "/", time.time()
        pairs = []
        for cookie in self.scoped_state()["cookies"]:
            domain = str(cookie.get("domain", "")).lower()
            if domain.startswith("."):
                if not (host == domain[1:] or host.endswith(domain)):
                    continue
            elif host != domain:
                continue
            if not path.startswith(cookie.get("path") or "/"):
                continue
            if cookie.get("secure") and parsed.scheme != "https":
                continue
            expires = cookie.get("expires", -1)
            if isinstance(expires, (int, float)) and 0 < expires < now:
                continue
            pairs.append(f"{cookie['name']}={cookie['value']}")
        return "; ".join(pairs) or None

    def context_options(self) -> dict:
        """Playwright new_context keyword arguments: session plus pinned fingerprint."""
        options = {"storage_state": self.scoped_state()}
        for key in ("user_agent", "locale", "timezone_id", "viewport"):
            if self.fingerprint.get(key):
                options[key] = self.fingerprint[key]
        return options

    def merge(self, state: dict) -> bool:
        """Fold a refreshed storage state into the profile; True when it changed."""
        scoped = Profile(self.name, self.sites, state=state).scoped_state()
        if not scoped["cookies"] and not scoped["origins"]:
            return False
        before = json.dumps(self.state, sort_keys=True)
        keep = {(c.get("name"), c.get("domain"), c.get("path")): c for c in self.state.get("cookies", [])}
        for cookie in scoped["cookies"]:
            keep[(cookie.get("name"), cookie.get("domain"), cookie.get("path"))] = cookie
        origins = {o.get("origin"): o for o in self.state.get("origins", [])}
        origins.update({o.get("origin"): o for o in scoped["origins"]})
        self.state = {"cookies": list(keep.values()), "origins": list(origins.values())}
        return json.dumps(self.state, sort_keys=True) != before


class ProfileStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or STORE).expanduser()

    def _path(self, name: str) -> Path:
        if not NAME.match(name or ""):
            raise ProfileError("Profile names are lowercase letters, digits, '.', '_' and '-'")
        return self.root / (name + ".fsp")

    def save(self, profile: Profile) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        path = self._path(profile.name)
        profile.updated_at = time.time()
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_seal(asdict(profile), profile.name))
        os.replace(temporary, path)

    def load(self, name: str) -> Profile:
        path = self._path(name)
        if not path.exists():
            raise ProfileError(f"No profile named {name!r}; create it with: frankensurf profile-login {name} --site <site>")
        return Profile(**_open(path.read_bytes(), name))

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.root.glob("*.fsp")) if self.root.exists() else []:
            try:
                profile = self.load(path.stem)
            except ProfileError:
                out.append({"name": path.stem, "status": "unreadable with this key"})
                continue
            out.append({"name": profile.name, "sites": profile.sites, "sharing": profile.sharing,
                        "version": profile.version, "cookies": len(profile.state.get("cookies", [])),
                        "updated_at": profile.updated_at})
        return out


# The profile active for the read running in this task (set by Runtime.read).
ACTIVE: ContextVar[Profile | None] = ContextVar("frankensurf_profile", default=None)


async def login(store: ProfileStore, name: str, sites: list[str], *, sharing: str = "local",
                start_url: str | None = None, timeout_seconds: float = 600, headless: bool = False,
                on_ready=None) -> Profile:
    """Open a visible FrankenSurf Chromium, let the person sign in, capture the session.

    The person closes the window when done (or ``on_ready`` returns True in tests).
    The latest snapshot taken while the window was open is what gets stored.
    """
    import asyncio
    from playwright.async_api import async_playwright
    if sharing not in SHARING:
        raise ProfileError("sharing must be 'local' or 'hosted'")
    sites = sorted({site.lower().removeprefix("www.").strip("/") for site in sites if site})
    if not sites:
        raise ProfileError("A profile needs at least one --site")
    try:
        existing = store.load(name)
    except ProfileError:
        existing = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(
            **(existing.context_options() if existing else {}), locale="en-AU")
        page = await context.new_page()
        await page.goto(start_url or "https://" + sites[0] + "/")
        fingerprint = {"user_agent": await page.evaluate("navigator.userAgent"),
                       "locale": await page.evaluate("navigator.language"),
                       "timezone_id": await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                       "viewport": page.viewport_size}
        state, deadline = await context.storage_state(), time.monotonic() + timeout_seconds
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())
        browser.on("disconnected", lambda *_: closed.set())
        while not closed.is_set() and time.monotonic() < deadline:
            try:
                state = await context.storage_state()
                if on_ready is not None and await on_ready(page):
                    break
            except Exception:
                break
            try:
                await asyncio.wait_for(closed.wait(), 1.0)
            except TimeoutError:
                pass
        try:
            await browser.close()
        except Exception:
            pass
    profile = existing or Profile(name=name, sites=sites, sharing=sharing)
    profile.sites = sorted(set(profile.sites) | set(sites))
    profile.sharing = sharing
    profile.fingerprint = fingerprint
    profile.state = {"cookies": [], "origins": []} if existing is None else profile.state
    profile.merge(state)
    profile.version += 0 if existing is None else 1
    store.save(profile)
    return profile

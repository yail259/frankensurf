"""T3 signed identity: Web Bot Auth request signatures.

Web Bot Auth (IETF draft-meunier-web-bot-auth-architecture, verified by
Cloudflare, AWS, Akamai and Vercel) signs each request with an Ed25519 key
using HTTP Message Signatures (RFC 9421). Sites that verify it can let a
signed agent through instead of treating it as an anonymous bot.

The key is a private JWK at ``~/.config/frankensurf/web-bot-auth.jwk``
(override with ``FRANKENSURF_BOT_AUTH_KEY_FILE``), created by
``frankensurf bot-auth-init``. ``FRANKENSURF_SIGNATURE_AGENT`` is the HTTPS
origin that serves the public key directory at
``/.well-known/http-message-signatures-directory``;
``frankensurf bot-auth-directory`` prints that file. When no key exists,
nothing is signed. The private key never leaves this module.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_KEY_FILE = Path("~/.config/frankensurf/web-bot-auth.jwk")
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
LIFETIME_SECONDS = 300


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_file() -> Path:
    from .hosted_providers import _setting
    return Path(_setting("bot_auth_key_file") or DEFAULT_KEY_FILE).expanduser()


def signature_agent() -> str | None:
    from .hosted_providers import _setting
    value = _setting("signature_agent")
    return value.rstrip("/") if value else None


def public_jwk(private_jwk: dict) -> dict:
    return {"kty": "OKP", "crv": "Ed25519", "x": private_jwk["x"]}


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint: the keyid Web Bot Auth verifiers look up."""
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
                           separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def generate(path: Path | None = None) -> dict:
    """Create a new Ed25519 key (mode 0600). Refuses to overwrite one."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = Path(path or key_file()).expanduser()
    if path.exists():
        raise FileExistsError(str(path))
    key = Ed25519PrivateKey.generate()
    d = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                          serialization.NoEncryption())
    x = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk = {"kty": "OKP", "crv": "Ed25519", "d": _b64url(d), "x": _b64url(x)}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(jwk, stream)
    return public_jwk(jwk)


def load(path: Path | None = None) -> dict | None:
    try:
        jwk = json.loads(Path(path or key_file()).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(jwk, dict) or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519"
            or not isinstance(jwk.get("d"), str) or not isinstance(jwk.get("x"), str)):
        return None
    return jwk


def directory(jwk: dict) -> dict:
    """The public key directory to serve at DIRECTORY_PATH on the agent origin."""
    return {"keys": [public_jwk(jwk)]}


def _authority(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    default = {"http": 80, "https": 443}.get(parts.scheme)
    return host if parts.port in (None, default) else f"{host}:{parts.port}"


def sign(url: str, jwk: dict, agent: str | None = None, *, now: int | None = None,
         nonce: str | None = None, label: str = "sig1") -> dict:
    """Return the Signature-Agent, Signature-Input and Signature headers for one request."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    created = int(time.time() if now is None else now)
    nonce = nonce or _b64url(secrets.token_bytes(32))
    components = ['"@authority"']
    lines = ['"@authority": ' + _authority(url)]
    headers = {}
    if agent:
        headers["Signature-Agent"] = '"' + agent + '"'
        components.append('"signature-agent"')
        lines.append('"signature-agent": ' + headers["Signature-Agent"])
    params = ("(" + " ".join(components) + ")"
              + f';created={created};expires={created + LIFETIME_SECONDS}'
              + f';keyid="{thumbprint(jwk)}";alg="ed25519";nonce="{nonce}";tag="web-bot-auth"')
    lines.append('"@signature-params": ' + params)
    key = Ed25519PrivateKey.from_private_bytes(_unb64url(jwk["d"]))
    signature = key.sign("\n".join(lines).encode())
    headers["Signature-Input"] = label + "=" + params
    headers["Signature"] = label + "=:" + base64.b64encode(signature).decode() + ":"
    return headers


# The signer for the Core HTTP read running in this task, if any. Core's
# shared httpx client signs each outgoing request (every redirect hop
# included) through sign_hop, so each hop carries its own @authority.
SIGNER: ContextVar[tuple[dict, str | None] | None] = ContextVar("frankensurf_bot_auth", default=None)


async def sign_hop(request: httpx.Request) -> None:
    signer = SIGNER.get()
    if signer is not None:
        request.headers.update(sign(str(request.url), *signer))


def current_signer():
    """(key, signature agent) for Core HTTP reads, or None when no key exists."""
    jwk = load()
    return (jwk, signature_agent()) if jwk else None

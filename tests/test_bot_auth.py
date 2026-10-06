"""T3 signed identity: Web Bot Auth signatures on Core HTTP reads."""
import base64
import json
import re

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from frankensurf import bot_auth, cli
from frankensurf.runtime import Runtime, WebPolicy

# RFC 9421 Appendix B.1.4 test key; Cloudflare's public verifier accepts it.
TEST_KEY = {"kty": "OKP", "crv": "Ed25519", "d": "n4Ni-HpISpVObnQMW0wOhCKROaIKqKtW_2ZYb2p9KcU",
            "x": "JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs"}
PAGE = "<html><title>Shop</title><body>" + "<p>Listing text with price $90.</p>" * 20 + "</body></html>"


def verify(headers, authority, agent=None):
    """Rebuild the RFC 9421 signature base and check it with the public key."""
    params = headers["Signature-Input"].split("=", 1)[1]
    lines = ['"@authority": ' + authority]
    if agent:
        lines.append('"signature-agent": "' + agent + '"')
    lines.append('"@signature-params": ' + params)
    signature = base64.b64decode(re.fullmatch(r"sig1=:(.+):", headers["Signature"]).group(1))
    public = Ed25519PublicKey.from_public_bytes(bot_auth._unb64url(TEST_KEY["x"]))
    public.verify(signature, "\n".join(lines).encode())
    return params


def test_keyid_is_the_rfc7638_thumbprint():
    assert bot_auth.thumbprint(TEST_KEY) == "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"


def test_signature_covers_authority_and_agent_and_verifies():
    headers = bot_auth.sign("https://Shop.Example.com:443/item?x=1", TEST_KEY,
                            "https://agent.example", now=1_700_000_000, nonce="n")
    assert headers["Signature-Agent"] == '"https://agent.example"'
    params = verify(headers, "shop.example.com", "https://agent.example")
    assert params == ('("@authority" "signature-agent");created=1700000000;expires=1700000300;'
                      'keyid="poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U";alg="ed25519";'
                      'nonce="n";tag="web-bot-auth"')
    plain = bot_auth.sign("http://localhost:8080/", TEST_KEY, now=1)
    assert "Signature-Agent" not in plain
    verify(plain, "localhost:8080")


def test_generated_key_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / "key.jwk"
    public = bot_auth.generate(path)
    assert path.stat().st_mode & 0o777 == 0o600 and "d" not in public
    assert bot_auth.load(path)["x"] == public["x"]
    with pytest.raises(FileExistsError):
        bot_auth.generate(path)
    assert bot_auth.directory(bot_auth.load(path)) == {"keys": [public]}


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    path = tmp_path / "web-bot-auth.jwk"
    path.write_text(json.dumps(TEST_KEY))
    monkeypatch.setenv("FRANKENSURF_BOT_AUTH_KEY_FILE", str(path))
    monkeypatch.setenv("FRANKENSURF_SIGNATURE_AGENT", "https://agent.example/")
    return path


async def test_http_reads_are_signed_on_every_hop(tmp_path, signing_key):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "old.example.com":
            return httpx.Response(301, headers={"location": "https://shop.example.com/item"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    async with Runtime(tmp_path / "state", transport=httpx.MockTransport(handler)) as web:
        result = await web.read("https://old.example.com/item")
    assert result["receipt"]["status"] == "observed"
    assert [request.url.host for request in seen] == ["old.example.com", "shop.example.com"]
    for request in seen:
        verify(request.headers, request.url.host, "https://agent.example")


async def test_unsigned_when_disabled_or_without_a_key(tmp_path, signing_key, monkeypatch):
    seen = []
    transport = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(
        200, text=PAGE, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path / "state", transport=transport) as web:
        await web.read("https://shop.example.com/a", WebPolicy(sign_requests=False))
        monkeypatch.setenv("FRANKENSURF_BOT_AUTH_KEY_FILE", str(tmp_path / "missing.jwk"))
        await web.read("https://shop.example.com/b")
    assert all("signature" not in request.headers for request in seen) and len(seen) == 2
    with pytest.raises(ValueError):
        WebPolicy(sign_requests="yes")


def test_cli_creates_the_key_and_prints_the_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FRANKENSURF_BOT_AUTH_KEY_FILE", str(tmp_path / "k.jwk"))
    import asyncio
    created = asyncio.run(cli.run(cli.parse_args(["bot-auth-init"])))
    assert created["keyid"] == bot_auth.thumbprint(created["directory"]["keys"][0])
    shown = asyncio.run(cli.run(cli.parse_args(["bot-auth-directory"])))
    assert shown == created["directory"] and "d" not in shown["keys"][0]

"""Prepare an owner-controlled LOCAL_ONLY visible browser; login stays manual.

Default is a reviewable plan with no mutations. --launch opens a dedicated
headful Chromium profile using its ordinary sandbox and a loopback-only CDP
endpoint. It never changes or copies a user's existing browser profile.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from frankensurf.identity import IdentityRegistry, _private_dir
from frankensurf.runtime import Runtime


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--identity", required=True)
    p.add_argument("--login-url", required=True)
    p.add_argument("--domain", action="append", required=True)
    p.add_argument("--image-domain", action="append", default=[])
    p.add_argument("--path-prefix", action="append", required=True)
    p.add_argument("--snapshot-root", action="append", required=True)
    p.add_argument("--port", type=int, default=9341)
    p.add_argument("--geography", default="Sydney")
    p.add_argument("--launch", action="store_true")
    return p


async def run(args):
    registry = IdentityRegistry()
    endpoint = "http://127.0.0.1:" + str(args.port)
    if not 1024 <= args.port <= 65535:
        raise ValueError("invalid port")
    parsed = urlparse(args.login_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname not in args.domain:
        raise ValueError("login URL must use HTTPS on an explicitly enrolled domain")
    profile = Path.home()/".frankensurf"/"profiles"/args.identity
    executor = "drive-" + args.identity
    executor_options = dict(endpoint=endpoint,user_data_dir=str(profile),profile_directory="Default",
                            network_context="drive-local",geography=args.geography)
    identity_options = dict(executor_id=executor,domains=args.domain,image_domains=args.image_domain,
                            authority_mode="LOCAL_ONLY",snapshot_policy={"path_prefixes":args.path_prefix,"root_selectors":args.snapshot_root})
    # Validate the exact proposed policy before touching files or launching.
    registry._executor_record(executor,**executor_options)
    registry._identity_record(args.identity,**identity_options)
    plan = {"identity":args.identity,"authority":"LOCAL_ONLY","read_mode":"owner_visible_snapshot",
            "browser":"dedicated headful local Chromium","profile_export":False,"cloud_execution":False,
            "manual_login_required":True,"login_verified":False,"automated_navigation":False,
            "sandbox_disabled":False,"tls_checks_disabled":False,"mutation_requested":args.launch}
    if not args.launch:
        print(json.dumps({"status":"plan_only",**plan},indent=2))
        return
    if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        raise RuntimeError("local graphical display is unavailable")
    from playwright.async_api import async_playwright
    browser_process = None
    with socket.socket() as probe:
        probe.settimeout(.3)
        already_listening = probe.connect_ex(("127.0.0.1",args.port)) == 0
    async with async_playwright() as pw:
        if not already_listening:
            _private_dir(profile.parent)
            _private_dir(profile)
            binary = pw.chromium.executable_path
            if not Path(binary).is_file():
                raise RuntimeError("installed local Chromium is unavailable")
            browser_process = subprocess.Popen([binary,"--enable-automation","--no-first-run",
                "--no-default-browser-check",f"--user-data-dir={profile}","--profile-directory=Default",
                "--remote-debugging-address=127.0.0.1",f"--remote-debugging-port={args.port}",args.login_url],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        browser = None
        for _ in range(50):
            try:
                browser = await pw.chromium.connect_over_cdp(endpoint,timeout=500)
                break
            except Exception:
                if browser_process and browser_process.poll() is not None:
                    raise RuntimeError("headful sandboxed Chromium did not start") from None
                await asyncio.sleep(.1)
        if browser is None:
            raise RuntimeError("dedicated local browser did not become reachable")
        session = await browser.new_browser_cdp_session()
        try:
            argv = (await session.send("Browser.getBrowserCommandLine")).get("arguments",[])
            flags = dict(arg.split("=",1) for arg in argv if arg.startswith("--") and "=" in arg)
            if (Runtime._identity_path(flags.get("--user-data-dir", "")) != Runtime._identity_path(str(profile))
                    or flags.get("--profile-directory", "Default") != "Default"
                    or any(arg in argv for arg in ("--no-sandbox","--disable-web-security","--ignore-certificate-errors"))
                    or len(browser.contexts) != 1
                    or (await session.send("Target.getBrowserContexts")).get("browserContextIds")):
                raise RuntimeError("existing browser does not match the reviewed dedicated profile")
        finally:
            await session.detach()
        registry.enroll_executor(executor,**executor_options)
        registry.enroll_identity(args.identity,**identity_options)
        print(json.dumps({"status":"owner_login_ready",**plan,
                          "identity_status":registry.status(args.identity),
                          "next_step":"Sign in manually in the separate local browser, then open a Marketplace listing. Password and MFA stay with you."},indent=2))
        # Stop Playwright's observer connection; leave the owner's browser open.


if __name__ == "__main__":
    try:
        asyncio.run(run(parser().parse_args()))
    except Exception:
        print(json.dumps({"status":"failed","failure":"LOCAL_BROWSER_SETUP_FAILED",
                          "next_step":"Inspect local browser/display availability and the dedicated profile binding; no raw exception or profile data exported."}))
        raise SystemExit(1) from None

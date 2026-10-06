"""Controlled local identity+photo canary; never reads a user's browser profile.

The cookie is a synthetic fixture credential. The owned Chromium profile is private
and remains separate from all user profiles. Only the process launched here stops.
"""
from __future__ import annotations
import argparse
import asyncio
import io
import json
import os
import socket
import subprocess
import threading
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

import httpx
from PIL import Image
from playwright.async_api import async_playwright
from frankensurf import Runtime,WebPolicy
from frankensurf.identity import IdentityRegistry


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


async def run(args):
    output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    private=output/"private-canary"
    private.mkdir(mode=0o700,exist_ok=True)
    profile=private/"owned-profile"
    profile.mkdir(mode=0o700,exist_ok=True)
    events=[]
    picture=io.BytesIO()
    Image.new("RGB",(61,37),"green").save(picture,"PNG")
    image=picture.getvalue()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            events.append({"path":self.path,"method":"POST","authenticated":"fs_canary_session=controlled_fixture" in self.headers.get("Cookie","")})
            self.send_response(403);self.end_headers()
        def do_GET(self):
            authenticated="fs_canary_session=controlled_fixture" in self.headers.get("Cookie","")
            events.append({"path":self.path,"method":"GET","authenticated":authenticated})
            if self.path=="/login":
                self.send_response(200);self.send_header("Set-Cookie","fs_canary_session=controlled_fixture; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(b"<title>Fixture login</title><p>Controlled session established</p>")
            elif self.path=="/redirect-outside":
                self.send_response(302);self.send_header("Location",f"http://localhost:{self.server.server_port}/outside");self.end_headers()
            elif self.path=="/image-redirect":
                self.send_response(302);self.send_header("Location",f"http://localhost:{self.server.server_port}/outside-image");self.end_headers()
            elif self.path=="/private.png" and authenticated:
                self.send_response(200);self.send_header("Content-Type","image/png");self.send_header("Content-Length",str(len(image)));self.end_headers();self.wfile.write(image)
            elif self.path in {"/script-only","/template-only"} and authenticated:
                text='<title>Script only</title><div id="root"></div><script>document.getElementById("root").textContent="Dynamic listing";fetch("/protected-json");</script>' if self.path=="/script-only" else '<title>Unresolved</title><p>{{ listing.title }}</p><script>window.renderListing();</script>'
                self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(text.encode())
            elif self.path=="/protected-json" and authenticated:
                data={"id":"fixture-product","title":"Synthetic controlled bike","description":"Test only", "variants":[{"id":"fixture-variant","title":"Fixture","price":20000,"available":True}],"images":[f"http://127.0.0.1:{self.server.server_port}/private.png"]}
                self.send_response(200);self.send_header("Content-Type","application/json");self.end_headers();self.wfile.write(json.dumps(data).encode())
            elif self.path=="/protected" and authenticated:
                self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers()
                page=f"""<title>Controlled protected listing</title><main id="authenticated"><h1>Fixture commuter bike</h1><img src="/private.png"></main><script>window.open('http://localhost:{self.server.server_port}/outside-popup');new Worker('/worker.js');fetch('http://localhost:{self.server.server_port}/outside-fetch');fetch('/write',{{method:'POST',body:'synthetic attempted mutation'}});</script>"""
                self.wfile.write(page.encode())
            else:
                self.send_response(401);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(b'<form id="login">Authentication required</form>')
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f"http://127.0.0.1:{server.server_port}"
    debug_port=unused_port()
    endpoint=f"http://127.0.0.1:{debug_port}"
    registry=private/"identities.json"
    reg=IdentityRegistry(registry)
    results=[]
    browser_process=None
    driver=None
    boot_browser=None
    def record(name,result,expected):
        receipt=result.get("receipt",{})
        actual=receipt.get("failure",{}).get("code") if receipt.get("failure") else receipt.get("status")
        item={"case":name,"expected":expected,"actual":actual,"passed":actual==expected,"receipt":receipt,"images":result.get("images",[])}
        results.append(item)
        print(json.dumps({k:item[k] for k in ("case","expected","actual","passed")}),flush=True)
    def no_independent_http(request):
        raise AssertionError("Named identity used independent HTTP")
    try:
        driver=await async_playwright().start()
        executable=driver.chromium.executable_path
        browser_process=subprocess.Popen([executable,"--headless=new","--no-sandbox","--disable-dev-shm-usage","--enable-automation","--no-first-run","--disable-background-networking",f"--user-data-dir={profile}","--profile-directory=Default","--remote-debugging-address=127.0.0.1",f"--remote-debugging-port={debug_port}","about:blank"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for attempt in range(50):
            try:
                boot_browser=await driver.chromium.connect_over_cdp(endpoint,timeout=500)
                break
            except Exception:
                await asyncio.sleep(0.1)
        if boot_browser is None: raise RuntimeError("Owned canary Chromium did not become ready")
        context=boot_browser.contexts[0]
        page=await context.new_page();await page.goto(base+"/login");await page.close()
        reg.enroll_executor("canary-local",endpoint=endpoint,user_data_dir=str(profile),profile_directory="Default",profile_version=1,network_context="controlled-loopback")
        reg.enroll_identity("canary-private",executor_id="canary-local",domains=["127.0.0.1"],image_domains=["127.0.0.1"],authority_mode="LOCAL_ONLY",auth_check={"url":base+"/protected","authenticated_selector":"#authenticated","login_selector":"#login"})
        policy=WebPolicy(identity="canary-private",include_images=True,timeout_seconds=3,settle_ms=0)
        async with Runtime(private/"state",identity_registry=registry,transport=httpx.MockTransport(no_independent_http)) as web:
            good=await web.read(base+"/protected",policy)
            record("authenticated_page_and_photo",good,"observed")
            assert good["images"][0]["status"]=="decoded"
            assert good["images"][0]["width"]==61
            assert good["receipt"]["authentication"]=="verified"
            json_result=await web.extract(base+"/protected-json","json",policy)
            record("authenticated_structured_product",json_result,"observed")
            assert json_result["structured"]["variants"][0]["id"]=="fixture-variant"
            assert json_result["images"][0]["status"]=="decoded"
            # Identity tabs run page scripts like a normal tab (v0.8.0).
            script_only=await web.read(base+"/script-only",policy)
            record("script_rendered_page",script_only,"observed")
            assert "Dynamic listing" in script_only["text"]
            captured=await web.read(base+"/script-only",WebPolicy(identity="canary-private",capture_json_responses=True,timeout_seconds=5,settle_ms=500))
            record("identity_page_json_capture",captured,"observed")
            assert any(item["url"].endswith("/protected-json") and item["data"]["id"]=="fixture-product" for item in captured["captured_json"]["items"])
            template_only=await web.read(base+"/template-only",policy)
            record("template_page_with_broken_script",template_only,"observed")
            cached=await web.read(base+"/protected",WebPolicy(identity="canary-private",freshness="cached",include_images=True,timeout_seconds=3,settle_ms=0))
            record("cache_after_identity_revalidation",cached,"observed")
            assert cached["receipt"]["cache_hit"]
            photos=await web.download_images([base+"/private.png"],policy)
            results.append({"case":"standalone_authenticated_photo","passed":photos[0]["status"]=="decoded","images":photos})
            redirect=await web.read(base+"/redirect-outside",policy)
            record("off_domain_page_redirect",redirect,"IDENTITY_DOMAIN_DENIED")
            redirected_photo=await web.download_images([base+"/image-redirect"],policy)
            results.append({"case":"off_domain_image_redirect","passed":redirected_photo[0].get("failure") in {"IDENTITY_POLICY_DENIED","IDENTITY_DOMAIN_DENIED"},"images":redirected_photo})
        if args.trade_python:
            template=json.loads(Path(args.trade_template).read_text())[0]
            listing={**template,"wtb_listing_id":"canary:controlled","external_id":"controlled","source":"canary","url":base+"/protected","title":"Synthetic fixture bike; no product identification","description":"Controlled test only, not a real item", "images":[{"url":base+"/private.png"}],"attributes":{"source_adapter":"json","variant_id":"fixture-variant","price_unit":"minor","acquisition_url":base+"/protected-json"},"evidence_refs":[],"status":"UNKNOWN","coverage_status":"summary_only"}
            listing["price"]={**listing["price"],"amount":200,"currency":"AUD","text":"A$200 fixture"}
            listing_file=private/"trade-listing.json"
            listing_file.write_text(json.dumps([listing]))
            destination=output/"trade-pipeline.json"
            env={**os.environ,"WTB_HOME":str(private/"trade-source"),"TRADE_HOME":str(private/"trade-ledger")}
            process=await asyncio.create_subprocess_exec(args.trade_python,"-m","trade.pipeline","--file",str(listing_file),"--output",str(destination),"--state-dir",str(private/"trade-frankensurf"),"--identity","canary-private","--identity-registry",str(registry),"--refresh","--fetch-images","--no-log",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
            stdout,stderr=await asyncio.wait_for(process.communicate(),timeout=60)
            if process.returncode: raise RuntimeError("Trade controlled pipeline failed: "+stderr.decode()[-1000:])
            pipeline=json.loads(destination.read_text())["cycles"][0]
            outcome=pipeline["results"][0]
            acquisition=outcome["listing"]["attributes"]["acquisition_receipts"][-1]
            passed=outcome["decision"]=="RESEARCH" and outcome["decoded_photos"]==1 and acquisition["identity"]=="canary-private"
            results.append({"case":"trade_named_identity_pipeline","passed":passed,"decision":outcome["decision"],"decoded_photos":outcome["decoded_photos"],"identity":acquisition["identity"],"artifact":str(destination),"scope":"Synthetic cookie-protected structured variant is fresh availability evidence; missing vision and SKU force RESEARCH."})
        wrong=private/"wrong-profile";wrong.mkdir(exist_ok=True)
        reg.enroll_executor("canary-local",endpoint=endpoint,user_data_dir=str(wrong),profile_directory="Default",profile_version=2,network_context="controlled-loopback")
        async with Runtime(private/"state",identity_registry=registry,transport=httpx.MockTransport(no_independent_http)) as web:
            wrong_result=await web.read(base+"/protected",policy)
            record("wrong_profile_binding",wrong_result,"IDENTITY_PROFILE_MISMATCH")
        reg.enroll_executor("canary-local",endpoint=endpoint,user_data_dir=str(profile),profile_directory="Default",profile_version=3,network_context="controlled-loopback")
        resolved=reg.resolve("canary-private",base+"/protected")
        with reg.lease(resolved):
            async with Runtime(private/"lease-state",identity_registry=registry,transport=httpx.MockTransport(no_independent_http)) as web:
                leased=await web.read(base+"/protected",policy)
                record("simultaneous_identity_lease",leased,"IDENTITY_IN_USE")
        # Seed evidence for the restored executor generation, so expiry tests a
        # genuinely matching cache rather than an already-invalid old generation.
        async with Runtime(private/"state",identity_registry=registry,transport=httpx.MockTransport(no_independent_http)) as web:
            restored=await web.read(base+"/protected",policy)
            record("restored_profile_generation_observation",restored,"observed")
            current_resolved=reg.resolve("canary-private",base+"/protected")
            matching_key=web._cache_key(base+"/protected",None,WebPolicy(identity="canary-private",freshness="cached",include_images=True,timeout_seconds=3,settle_ms=0),current_resolved)
            assert (private/"state/cache"/(matching_key+".json")).exists()
        await context.clear_cookies()
        async with Runtime(private/"state",identity_registry=registry,transport=httpx.MockTransport(no_independent_http)) as web:
            expired=await web.read(base+"/protected",WebPolicy(identity="canary-private",freshness="cached",include_images=True,timeout_seconds=3,settle_ms=0))
            record("expired_auth_before_cache",expired,"IDENTITY_REAUTH_REQUIRED")
        browser_process.terminate();browser_process.wait(timeout=5)
        async with Runtime(private/"state",identity_registry=registry,transport=httpx.MockTransport(no_independent_http)) as web:
            offline=await web.read(base+"/protected",policy)
            record("offline_no_fallback",offline,"IDENTITY_EXECUTOR_OFFLINE")
        # Top-level navigation never leaves the identity's domains; subresources
        # and page-script requests load as in a normal tab.
        results.append({"case":"no_off_scope_top_level_navigation","passed":not any(x["path"] in {"/outside","/outside-image"} for x in events)})
        results.append({"case":"page_scripts_run_like_a_normal_tab","passed":any(x["path"] in {"/worker.js","/outside-fetch"} for x in events)})
    except BaseException as exc:
        results.append({"case":"canary_completion","passed":False,"error_type":type(exc).__name__})
        raise
    finally:
        if browser_process and browser_process.poll() is None:
            browser_process.terminate()
            try: browser_process.wait(timeout=5)
            except subprocess.TimeoutExpired: browser_process.kill();browser_process.wait(timeout=5)
        if driver: await driver.stop()
        server.shutdown();server.server_close()
        report={"schema":"frankensurf.controlled-identity-canary/v1","generated_at":datetime.now(timezone.utc).isoformat(),"scope":"Isolated owned local Chromium and synthetic cookie-gated HTML/JSON fixture, with page scripts enabled and top-level navigation scoped; not real Marketplace authentication.","results":results,"server_events":events,"outside_requests":sum(x["path"].startswith("/outside") for x in events),"owned_browser_stopped":browser_process is None or browser_process.poll() is not None,"paid_spend_usd":0,"all_passed":bool(results) and all(x["passed"] for x in results)}
        (output/"identity-canary.json").write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps({"all_passed":report["all_passed"],"cases":len(results),"outside_requests":report["outside_requests"],"owned_browser_stopped":report["owned_browser_stopped"]}),flush=True)
    if not all(x["passed"] for x in results): raise SystemExit(1)

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--output",required=True)
    p.add_argument("--trade-python")
    p.add_argument("--trade-template")
    args=p.parse_args()
    if args.trade_python and not args.trade_template:p.error("--trade-python requires --trade-template")
    asyncio.run(run(args))

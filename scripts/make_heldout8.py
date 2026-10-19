"""Build held-out set 8 from candidates written for it (scripts/heldout8-candidates.json):
chosen from general knowledge by Claude on 20 October 2026, before any page was read,
to check the October ladder changes blind. Every host and brand already read (every
benchmark row file, sets 1 to 7 included, spot checks, corpora, route seeds) is left out
by scripts/used_hosts.py.

  python scripts/make_heldout8.py > scripts/toolbench-heldout8.json
"""
import html
import json
import subprocess
import sys
from pathlib import Path

from make_heldout6 import brand

HERE = Path(__file__).resolve().parent


def main():
    used = set(json.loads(subprocess.run([sys.executable, str(HERE / "used_hosts.py")],
                                         capture_output=True, text=True, check=True).stdout))
    used_brands = {brand(h) for h in used if "." in h}
    groups = json.loads((HERE / "heldout8-candidates.json").read_text())["result"]
    by_kind, seen_sites, seen_brands = {}, set(), set()
    for group in groups:
        for candidate in group["candidates"]:
            url = html.unescape(candidate["url"]).strip()
            if not url.startswith(("https://", "http://")):
                continue
            host = url.split("/")[2].lower().removeprefix("www.")
            site = candidate["site"].strip().lower()
            if (host in used or brand(host) in used_brands or site in seen_sites
                    or brand(host) in seen_brands):
                continue
            seen_sites.add(site)
            seen_brands.add(brand(host))
            by_kind.setdefault(group["category"], []).append(
                {"site": site, "wall": group["category"], "url": url, "expect": candidate["expect"].strip().lower()})
    picked = []
    while len(picked) < 150 and any(by_kind.values()):
        for kind in sorted(by_kind):
            if by_kind[kind] and len(picked) < 150:
                picked.append(by_kind[kind].pop(0))
    note = ("Eighth held-out set, 20 October 2026: candidates written for it from general knowledge by Claude, "
            "before any page was read, to check the October ladder changes blind; " + str(len(used)) + " hosts "
            "and their brands already read are excluded. Same rules: 800+ characters and the expect pattern at "
            "least 3 times. Some URLs may be dead or wrong; that counts against every arm alike. Use once.")
    json.dump({"schema": "frankensurf.toolbench-heldout/v1", "note": note, "cases": picked},
              sys.stdout, indent=1, ensure_ascii=False)
    print(len(picked), "picked", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Build held-out set 6: sites chosen from general knowledge before any page is read.

Candidates were proposed per category by agents working from memory only (no
web access), then checked by a second agent the same way; they are kept in
scripts/heldout6-candidates.json. Every host and brand that any earlier set,
row file or spot check has read is left out automatically (scripts/used_hosts.py).

  python scripts/make_heldout6.py > scripts/toolbench-heldout6.json
"""
import html
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def brand(host):
    """The site's own name: ebay for ebay.co.uk, ebay.de and ebay.com.au alike."""
    parts = host.split(".")
    return parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "govt", "ac") else parts[-2]


def main():
    used = set(json.loads(subprocess.run([sys.executable, str(HERE / "used_hosts.py")],
                                         capture_output=True, text=True, check=True).stdout))
    used_brands = {brand(h) for h in used if "." in h}
    data = json.loads((HERE / "heldout6-candidates.json").read_text())
    groups = data["result"] if isinstance(data, dict) else data
    cases, seen_sites, seen_brands = [], set(), set()
    for group in groups:
        for candidate in group["candidates"]:
            url = html.unescape(candidate["url"]).strip()
            if not url.startswith(("https://", "http://")):
                continue
            host = url.split("/")[2].lower().removeprefix("www.")
            site = candidate["site"].strip().lower()
            # The same brand on another country's domain is not a fresh site.
            if (host in used or brand(host) in used_brands or site in seen_sites
                    or brand(host) in seen_brands):
                continue
            seen_sites.add(site)
            seen_brands.add(brand(host))
            cases.append({"site": site, "wall": group["category"], "url": url,
                          "expect": candidate["expect"].strip().lower()})
    note = ("Sixth held-out set, 10 October 2026: sites chosen from general knowledge before any of their pages "
            "was read (proposed and checked by agents without web access), none on an earlier set or spot "
            "check (" + str(len(used)) + " hosts and their brands excluded automatically). Same rules as "
            "before: 800+ characters and the expect pattern at least 3 times. Some URLs may be dead or wrong; "
            "that counts against every arm alike. Use once.")
    by_kind = {}
    for case in cases:
        by_kind.setdefault(case["wall"], []).append(case)
    picked = []
    while len(picked) < 150 and any(by_kind.values()):
        for kind in sorted(by_kind):
            if by_kind[kind] and len(picked) < 150:
                picked.append(by_kind[kind].pop(0))
    json.dump({"schema": "frankensurf.toolbench-heldout/v1", "note": note, "cases": picked},
              sys.stdout, indent=1, ensure_ascii=False)
    print(file=sys.stderr)
    print(len(cases), "candidates kept after excluding used hosts and brands;", len(picked), "picked",
          file=sys.stderr)


if __name__ == "__main__":
    main()

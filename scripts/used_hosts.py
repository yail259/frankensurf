"""Every host any earlier benchmark set, row file or spot check has read, so a new
held-out set can leave them out. Prints a JSON list.

  python scripts/used_hosts.py > /tmp/used-hosts.json
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
URL = re.compile(r'https?://[^\s"\'<>\\)]+')


def hosts_in(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    found = set()
    for url in URL.findall(text):
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if "." in host:
            found.add(host)
    return found


def main():
    used = set()
    for path in list((ROOT / "scripts").glob("toolbench-heldout*.json")) + list((ROOT / "benchmarks").rglob("*.json*")):
        used |= hosts_in(path)
    # Hosts that every set links to (services, tools) are not sites under test.
    json.dump(sorted(used), sys.stdout, indent=0)
    print(f"{len(used)} hosts", file=sys.stderr)


if __name__ == "__main__":
    main()

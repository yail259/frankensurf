#!/usr/bin/env bash
set -euo pipefail
repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec python3 "$repo_root/scripts/local_services.py" "$@"

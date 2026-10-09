#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
provider_dir="${FRANKENSURF_PROVIDER_DIR:-$HOME/.local/share/frankensurf/provider-venv}"
python3 -m venv "$provider_dir"
"$provider_dir/bin/python" -m pip install -r "$repo_dir/src/frankensurf/provider_requirements.txt"
"$provider_dir/bin/python" -m camoufox fetch official/152.0.4-beta.31  # keep in step with experimental.CAMOUFOX_BROWSER
"$provider_dir/bin/python" -m patchright install chromium
printf 'Provider environment ready. Override with FRANKENSURF_PROVIDER_PYTHON if needed.\n'

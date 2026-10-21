#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

install_frontend=true
case "${1:-}" in
  "") ;;
  --genlayer-only) install_frontend=false ;;
  *) echo "Usage: $0 [--genlayer-only]" >&2; exit 2 ;;
esac

# Create the project-only Python environment, then install exactly the checked-in lock.
uv venv --seed --python 3.12 .venv
uv pip sync --build-constraints requirements-genlayer-build.lock.txt --python .venv/bin/python requirements-genlayer.lock.txt
if [[ "$install_frontend" == true ]]; then
  npm ci
fi

export PATH="$PWD/.venv/bin:$PATH"
python --version
python -m pip --version
python -m pip show genlayer-test genlayer-py
python - <<'PY'
from importlib.metadata import distribution, version
import json, sys
assert sys.version_info[:2] == (3, 12), f"Expected Python 3.12, got {sys.version}"
expected = {"genlayer-test": "0.30.0rc2", "genlayer-py": "0.19.0rc2", "pytest": "9.1.1"}
for package, wanted in expected.items():
    actual = version(package)
    print(f"{package}=={actual}")
    assert actual == wanted, f"Expected {package}=={wanted}, got {actual}"
source = json.loads(distribution("genlayer-test").read_text("direct_url.json") or "{}")
commit = source.get("vcs_info", {}).get("commit_id")
assert commit == "d84591b58746a6202b56b720199ce7b9f12fd666", f"Unexpected GenLayer Testing Suite source: {commit}"
print(f"GenLayer Testing Suite source commit: {commit}")
PY
uv --version
case "$(uv --version)" in "uv 0.13.3 ("*) ;; *) echo "Unexpected uv version" >&2; exit 1 ;; esac
node --version
test "$(node --version)" = "v22.14.0"
gltest --help >/dev/null
echo "GenLayer test runner: $(command -v gltest) (gltest --help succeeded)"

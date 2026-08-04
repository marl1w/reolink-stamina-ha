#!/usr/bin/env bash
#
# Local stand-in for CI. Run this before installing an update to your Home Assistant.
#
#   ./scripts/check.sh          # lint + tests + frontend parse
#   ./scripts/check.sh --setup  # create .venv and install test dependencies first
#
# Home Assistant 2026.3+ requires Python 3.14.2 or newer. If your default python3 is
# older, point PYTHON at a newer one:
#
#   PYTHON="$(pyenv root)/versions/3.14.6/bin/python3" ./scripts/check.sh --setup
#
# The contract tests in tests/test_upstream_contract.py are the important ones: they
# assert that the real Reolink integration and reolink_aio still look the way this panel
# expects. Without CI running them weekly, run this yourself after every Home Assistant
# update — that is what turns an upstream change into a clear failure here instead of a
# broken panel in your sidebar.

set -euo pipefail

cd "$(dirname "$0")/.."
VENV=".venv"
FAILED=0

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok() { printf '\033[32m  ok\033[0m %s\n' "$1"; }
bad() {
  printf '\033[31m  FAILED\033[0m %s\n' "$1"
  FAILED=1
}

if [[ "${1:-}" == "--setup" ]]; then
  PYTHON="${PYTHON:-python3}"
  step "Creating $VENV with $("$PYTHON" --version)"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install -r requirements-test.txt ruff
  echo
  echo "Done. Now run ./scripts/check.sh"
  exit 0
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "No $VENV found. Run: ./scripts/check.sh --setup"
  exit 1
fi

PY="$VENV/bin/python"

step "Ruff lint"
if "$PY" -m ruff check .; then ok "lint"; else bad "lint"; fi

step "Ruff format"
if "$PY" -m ruff format --check .; then ok "format"; else bad "format"; fi

step "Tests"
if "$PY" -m pytest tests -q; then ok "tests"; else bad "tests"; fi

step "Frontend modules parse"
if command -v node >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  PARSE_FAILED=0
  # Copied to .mjs so node parses them as ES modules rather than scripts.
  while IFS= read -r file; do
    cp "$file" "$TMP/$(basename "${file%.js}").mjs"
    node --check "$TMP/$(basename "${file%.js}").mjs" || PARSE_FAILED=1
  done < <(find custom_components/reolink_stamina/frontend -name '*.js')
  if [[ $PARSE_FAILED -eq 0 ]]; then ok "frontend"; else bad "frontend"; fi
else
  printf '  skipped (node not installed)\n'
fi

step "Clip writer (FLV to MP4)"
# Real files rather than fixtures: ffmpeg builds the FLV, the panel's own code remuxes it,
# and ffprobe reports what came out. Nothing here needs an NVR.
if command -v node >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1 &&
  command -v ffprobe >/dev/null 2>&1; then
  if node tests/frontend/test_clip.mjs; then ok "clip writer"; else bad "clip writer"; fi
else
  printf '  skipped (needs node, ffmpeg and ffprobe)\n'
fi

echo
if [[ $FAILED -eq 0 ]]; then
  printf '\033[32mAll checks passed.\033[0m\n'
else
  printf '\033[31mSomething failed — see above.\033[0m\n'
  exit 1
fi

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

step "Stylesheets are closed"
# A backtick inside a /* css */ template literal ends the string, and what follows parses as
# code. Sometimes that is a syntax error the check above catches; sometimes it is valid
# JavaScript and the panel silently loses its styles. Caught here twice in one afternoon.
#
# The stylesheets in this project all end with a line of exactly `; so that is the end of the
# block, and any backtick before it is one that should not be there.
if "$PY" - <<'PYEOF'; then ok "stylesheets"; else bad "stylesheets"; fi
import pathlib, sys

OPEN = "/* css */ `"
CLOSE = "\n`;"

bad = []
for path in sorted(pathlib.Path("custom_components/reolink_stamina/frontend").rglob("*.js")):
    if "vendor" in str(path):
        continue
    text = path.read_text()
    at = text.find(OPEN)
    while at != -1:
        body_at = at + len(OPEN)
        end = text.find(CLOSE, body_at)
        if end == -1:
            bad.append(f"{path.name}: a /* css */ block is never closed")
        elif "`" in text[body_at:end]:
            line = text.count("\n", 0, body_at + text[body_at:end].index("`")) + 1
            bad.append(f"{path.name}:{line}: backtick inside a CSS template literal")
        at = text.find(OPEN, end if end != -1 else body_at)

for problem in bad:
    print(f"  {problem}")
sys.exit(1 if bad else 0)
PYEOF

step "Playback ladder"
# Pure decisions — which route to try next, and what to remember — so node alone runs them.
if command -v node >/dev/null 2>&1; then
  if node tests/frontend/test_routes.mjs; then ok "routes"; else bad "routes"; fi
else
  printf '  skipped (node not installed)\n'
fi

step "Folding toolbar"
# A decision made from a stream of scroll positions, with the clock injected so a flick and
# a drift can be told apart without a thumb or a browser.
if command -v node >/dev/null 2>&1; then
  if node tests/frontend/test_fold.mjs; then ok "fold"; else bad "fold"; fi
else
  printf '  skipped (node not installed)\n'
fi

step "Split layout"
# Whether the panel is wide enough to hold the list and the player side by side, and how far
# the divider between them may be dragged. Numbers, so no window has to be resized to see it.
if command -v node >/dev/null 2>&1; then
  if node tests/frontend/test_split.mjs; then ok "split"; else bad "split"; fi
else
  printf '  skipped (node not installed)\n'
fi

step "What's new"
# When the panel introduces itself: a dialog that opens when it should not is the most
# annoying thing a panel can do, and it is four lines with three states easy to confuse.
if command -v node >/dev/null 2>&1; then
  if node tests/frontend/test_whats_new.mjs; then ok "what's new"; else bad "what's new"; fi
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

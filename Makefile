# Every way of running this project, in one place.
#
# The virtualenv is not something to remember: every target that needs it uses it, and the
# one that does not exist yet says so rather than failing with an import error six lines
# down. `make` on its own lists what there is.
#
#   make setup     once, or after a Python upgrade
#   make check     what CI runs
#   make preview   the panel in a browser, no Home Assistant needed
#
# Home Assistant 2026.3+ needs Python 3.14.2 or newer. If the default python3 is older:
#
#   make setup PYTHON=$$(pyenv root)/versions/3.14.6/bin/python3

VENV    ?= .venv
PYTHON  ?= python3
PY      := $(VENV)/bin/python

# Overridable on the command line: `make preview PORT=9000 SEED=7`
PORT    ?= 8123
SEED    ?= 1
DAYS    ?= 60
JOURNAL ?=
ARGS    ?=

# Colours, but only when something is watching. A log full of escape codes helps nobody.
ifneq ($(shell test -t 1 && echo tty),)
  BOLD := \033[1m
  DIM  := \033[2m
  OFF  := \033[0m
else
  BOLD :=
  DIM  :=
  OFF  :=
endif

.DEFAULT_GOAL := help
.PHONY: help setup check lint format format-check test test-relevance frontend \
        preview replay validate clean distclean venv-check

# ---------------------------------------------------------------------- help

help: ## List the targets
	@printf '$(BOLD)Reolink Stamina$(OFF)\n\n'
	@grep -hE '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "} {printf "  $(BOLD)%-16s$(OFF) %s\n", $$1, $$2}'
	@printf '\n$(DIM)Variables: PORT=%s SEED=%s DAYS=%s JOURNAL=<path> ARGS=<extra>$(OFF)\n\n' \
	  "$(PORT)" "$(SEED)" "$(DAYS)"

# --------------------------------------------------------------------- setup

setup: ## Create the virtualenv and install the test dependencies
	@scripts/check.sh --setup

# Everything below needs the virtualenv, and saying so plainly beats a stack trace.
venv-check:
	@test -x $(PY) || { \
	  printf '$(BOLD)No virtualenv at $(VENV)$(OFF)\n  Run: make setup\n'; exit 1; }

# --------------------------------------------------------------------- checks

check: ## Everything CI runs: lint, format, tests, and the frontend checks
	@scripts/check.sh

lint: venv-check ## Ruff lint
	@$(PY) -m ruff check .

format: venv-check ## Ruff format, writing the files
	@$(PY) -m ruff format .

format-check: venv-check ## Ruff format, reporting only
	@$(PY) -m ruff format --check .

test: venv-check ## The Python test suite (ARGS passes through to pytest)
	@$(PY) -m pytest tests -q $(ARGS)

test-relevance: venv-check ## Only the "learn what is normal" tests
	@$(PY) -m pytest tests -q -k relevance $(ARGS)

frontend: ## The panel's own checks: modules parse, and the pure-logic suites
	@command -v node >/dev/null 2>&1 || { echo "node is not installed"; exit 1; }
	@set -e; for file in $$(find custom_components/reolink_stamina/frontend -name '*.js'); do \
	  cp "$$file" "/tmp/$$(basename $${file%.js}).mjs"; \
	  node --check "/tmp/$$(basename $${file%.js}).mjs"; \
	done; printf '  modules parse\n'
	@for suite in tests/frontend/test_*.mjs; do node "$$suite"; done

# ------------------------------------------------------------------ running

preview: venv-check ## Serve the real panel with sample data in a browser
	@$(PY) scripts/preview.py --port $(PORT) --seed $(SEED) --days $(DAYS) $(ARGS)

replay: venv-check ## Re-score a journal offline: make replay JOURNAL=path/to.db
	@test -n "$(JOURNAL)" || { \
	  printf 'Which journal? $(DIM)make replay JOURNAL=~/homeassistant/reolink_stamina_journal.db$(OFF)\n'; \
	  exit 1; }
	@$(PY) scripts/replay.py "$(JOURNAL)" $(ARGS)

validate: ## The HACS and hassfest checks CI runs, if Docker is here to run them
	@command -v docker >/dev/null 2>&1 || { \
	  printf 'Docker is not installed — these two only run in CI.\n'; exit 1; }
	@docker run --rm -v $(PWD):/github/workspace -e INPUT_CATEGORY=integration \
	  ghcr.io/hacs/action:main
	@docker run --rm -v $(PWD):/github/workspace ghcr.io/home-assistant/hassfest:latest

# ------------------------------------------------------------------ tidying

clean: ## Remove caches and compiled files, keeping the virtualenv
	@find . -path ./$(VENV) -prune -o -name '__pycache__' -type d -exec rm -rf {} +
	@rm -rf .pytest_cache .ruff_cache
	@printf '  caches gone\n'

distclean: clean ## Also remove the virtualenv
	@rm -rf $(VENV)
	@printf '  $(VENV) gone — run make setup\n'

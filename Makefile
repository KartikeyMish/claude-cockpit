.PHONY: test compile validate lint eval install uninstall release release-finish help
.DEFAULT_GOAL := test

PYTHON ?= python3
SHELL_FILES := install.sh uninstall.sh plugins/orchestrator/hooks/*.sh plugins/orchestrator/evals/*/scaffold.sh
PY_FILES := bin/ccx lib/cockpit/*.py plugins/orchestrator/hooks/*.py plugins/orchestrator/scripts/*.py scripts/*.py tests/*.py

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/	/'

test:            ## unit + integration tests (stdlib only); PYTHON=... picks the interpreter
	$(PYTHON) -m unittest discover -s tests -v

compile:         ## byte-compile every Python file with $(PYTHON) (catches syntax the minimum version lacks)
	$(PYTHON) -m py_compile $(PY_FILES)

validate:        ## Claude's own manifest/agent/hook validation
	claude plugin validate plugins/orchestrator
	claude plugin validate .

lint:            ## shell + python lint if the tools are installed
	@if command -v shellcheck >/dev/null; then shellcheck $(SHELL_FILES); else echo "shellcheck not installed"; fi
	@if command -v ruff >/dev/null; then ruff check bin/ccx lib scripts tests; else echo "ruff not installed"; fi

eval:            ## routing evals: one run per case, capped spend
	claude plugin eval plugins/orchestrator --scaffold --runs 1 --ablation none --allow-tools Edit Write --max-cost-usd 10

install:         ## install from this clone (plugin from the release channel, ccx, Ghostty)
	./install.sh --with-ghostty

uninstall:
	./uninstall.sh

# Flags for the release targets: DRY_RUN=1 rehearses, NO_TAP=1 skips the Homebrew tap,
# YES=1 pushes without asking (needed when stdin is not a terminal).
is_set = $(filter 1 yes true,$(1))
RELEASE_FLAGS = $(if $(call is_set,$(DRY_RUN)),--dry-run,) $(if $(call is_set,$(NO_TAP)),--no-tap,)

release:         ## cut a release: make release VERSION=x.y.z [DRY_RUN=1] [NO_TAP=1] [YES=1] (TAP_DIR defaults to ~/homebrew-tap)
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=x.y.z [DRY_RUN=1] [NO_TAP=1] [YES=1]" >&2; exit 2; }
	$(PYTHON) scripts/release.py cut $(VERSION) $(RELEASE_FLAGS) $(if $(call is_set,$(YES)),--yes,)

release-finish:  ## finish a pushed release (Release, stable, tap); safe to re-run: make release-finish VERSION=x.y.z [DRY_RUN=1] [NO_TAP=1]
	@test -n "$(VERSION)" || { echo "usage: make release-finish VERSION=x.y.z [DRY_RUN=1] [NO_TAP=1]" >&2; exit 2; }
	$(PYTHON) scripts/release.py finish $(VERSION) $(RELEASE_FLAGS)

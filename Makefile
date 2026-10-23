# ─────────────────────────────────────────────────────────────────────
# Moodle Backup Server — Makefile
# ─────────────────────────────────────────────────────────────────────
#   make help            list all targets
#   make doctor          full environment diagnostic
#   make install         editable install (dev)
#   make install-stable  non-editable install (prod)
#   make check           verify install incl. sudo PATH
#   make prod            reinstall-stable + check (one-shot)
# ─────────────────────────────────────────────────────────────────────

SHELL         := /bin/bash
.SHELLFLAGS   := -eu -o pipefail -c
.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

# ── User-overridable ────────────────────────────────────────────────
PYTHON  ?= python3
PIP     ?= $(PYTHON) -m pip
SUDO    ?= sudo
VERBOSE ?= 0

# ── Package identity ────────────────────────────────────────────────
PKG_NAME := moodle_backup_server
CMD      := backup-server
VERSION  := $(or $(shell sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py 2>/dev/null | head -1),unknown)

# ── Environment auto-detection ──────────────────────────────────────
IN_VENV  := $(if $(VIRTUAL_ENV),1,0)
IS_ROOT  := $(shell id -u)
STDLIB   := $(shell $(PYTHON) -c "import sysconfig; print(sysconfig.get_path('stdlib'))" 2>/dev/null)
PEP668   := $(shell test -n "$(STDLIB)" -a -f "$(STDLIB)/EXTERNALLY-MANAGED" && echo 1 || echo 0)

ifeq ($(IN_VENV),1)
  PIP_FLAGS :=
  ENV_NOTE  := venv — no PEP 668 restrictions
else ifeq ($(PEP668),1)
  PIP_FLAGS := --break-system-packages
  ENV_NOTE  := system Python is externally-managed — PEP 668 flag applied
  ifneq ($(IS_ROOT),0)
    ENV_NOTE += ; NOTE: not root — run `sudo make install` if it fails
  endif
else
  PIP_FLAGS :=
  ENV_NOTE  :=
endif

PIP_QUIET := $(if $(filter 1,$(VERBOSE)),,-q)

# ── Colours ─────────────────────────────────────────────────────────
C_RESET  := \033[0m
C_GREEN  := \033[32m
C_YELLOW := \033[33m
C_RED    := \033[31m
C_CYAN   := \033[36m
C_DIM    := \033[2m
C_BOLD   := \033[1m

define say
	@printf "$(C_CYAN)▶$(C_RESET) %s\n" "$(1)"
endef
define ok
	@printf "$(C_GREEN)✔$(C_RESET) %s\n" "$(1)"
endef
define warn
	@printf "$(C_YELLOW)⚠$(C_RESET) %s\n" "$(1)"
endef
define err
	@printf "$(C_RED)✘$(C_RESET) %s\n" "$(1)" >&2
endef

# ─────────────────────────────────────────────────────────────────────
# help — generated from `## comment` on each target
# ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@printf "$(C_BOLD)Moodle Backup Server$(C_RESET) — v$(VERSION)\n"
	@printf "$(C_DIM)  python : %s\n" "$(PYTHON)"
	@printf "  pip    : $(PIP) $(PIP_FLAGS)\n"
	@printf "  env    : %s\n" "$(ENV_NOTE)"
	@printf "  pkg    : %s\n" "$(PKG_NAME)"
	@printf "  cmd    : %s$(C_RESET)\n\n" "$(CMD)"
	@printf "$(C_BOLD)Targets:$(C_RESET)\n"
	@awk 'BEGIN {FS = ":.*## "} \
	      /^[a-zA-Z_-]+:.*## / { printf "  $(C_CYAN)%-20s$(C_RESET) %s\n", $$1, $$2 }' \
	     $(MAKEFILE_LIST)

# ─────────────────────────────────────────────────────────────────────
# Environment / install
# ─────────────────────────────────────────────────────────────────────
.PHONY: check-python
check-python:
	@$(PYTHON) -c "import sys; sys.exit(0 if sys.version_info >= (3,6) else 'Python 3.6+ required: ' + sys.version)"

.PHONY: install
install: check-python ## Editable install (dev — picks up source changes live)
	$(call say,Editable install of $(PKG_NAME) v$(VERSION))
	@$(PIP) install $(PIP_FLAGS) $(PIP_QUIET) -e .
	$(call ok,Installed → $(CMD))
	@command -v $(CMD) >/dev/null 2>&1 \
		&& printf "  $(C_GREEN)✔$(C_RESET) %s in PATH\n" "$(CMD)" \
		|| printf "  $(C_YELLOW)⚠$(C_RESET) %s not in PATH yet — run 'hash -r' or open a new shell\n" "$(CMD)"

.PHONY: install-stable
install-stable: check-python ## Non-editable install (prod — self-contained console script)
	$(call say,Stable install of $(PKG_NAME) v$(VERSION))
	@$(PIP) install $(PIP_FLAGS) $(PIP_QUIET) --force-reinstall --no-cache-dir .
	$(call ok,Installed → $(CMD))

.PHONY: uninstall
uninstall: ## Remove the package
	$(call say,Uninstalling $(PKG_NAME))
	-@$(PIP) uninstall $(PIP_FLAGS) -y $(PIP_QUIET) $(PKG_NAME)
	$(call ok,Uninstalled)

.PHONY: reinstall
reinstall: uninstall install ## Uninstall + editable install

.PHONY: reinstall-stable
reinstall-stable: uninstall install-stable ## Uninstall + stable install

.PHONY: dev
dev: install check ## Dev setup: editable install + verification

.PHONY: prod
prod: reinstall-stable check ## Prod setup: stable install + verification

# ─────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────
.PHONY: check
check: ## Verify install: user PATH, sudo PATH, subcommand help
	$(call say,Verifying installation)
	@set -e; \
	if command -v $(CMD) >/dev/null 2>&1; then \
		printf "  $(C_GREEN)✔$(C_RESET) %s in user PATH: %s\n" "$(CMD)" "$$(command -v $(CMD))"; \
	else \
		printf "  $(C_RED)✘$(C_RESET) %s NOT in user PATH\n" "$(CMD)"; \
		printf "    Add /usr/local/bin: echo 'export PATH=\"/usr/local/bin:\$$PATH\"' >> ~/.bashrc\n"; \
		exit 1; \
	fi
	@set -e; \
	if $(SUDO) -n true 2>/dev/null; then \
		if $(SUDO) -n command -v $(CMD) >/dev/null 2>&1; then \
			printf "  $(C_GREEN)✔$(C_RESET) sudo can find %s\n" "$(CMD)"; \
		else \
			printf "  $(C_RED)✘$(C_RESET) sudo CANNOT find %s\n" "$(CMD)"; \
			printf "    Fix: sudo visudo -f /etc/sudoers.d/00-secure-path\n"; \
			printf "    Add: Defaults secure_path=\"...:/usr/local/bin:...\"\n"; \
			exit 1; \
		fi; \
		$(SUDO) -n $(CMD) --help >/dev/null 2>&1 && \
			printf "  $(C_GREEN)✔$(C_RESET) sudo %s --help works\n" "$(CMD)"; \
	else \
		printf "  $(C_YELLOW)⚠$(C_RESET) passwordless sudo unavailable — skipping sudo PATH check\n"; \
		printf "    Verify manually: sudo %s --help\n" "$(CMD)"; \
	fi
	$(call ok,All checks passed)

.PHONY: doctor
doctor: ## Full environment diagnostic
	$(call say,Environment diagnostic)
	@printf "  $(C_BOLD)Python$(C_RESET):   %s\n" "$$($(PYTHON) --version 2>&1)"
	@printf "  $(C_BOLD)Pip$(C_RESET):      %s\n" "$$($(PIP) --version 2>&1 | head -1)"
	@printf "  $(C_BOLD)PEP668$(C_RESET):   %s\n" "$(if $(filter 1,$(PEP668)),externally-managed,not managed)"
	@printf "  $(C_BOLD)venv$(C_RESET):     %s\n" "$(if $(filter 1,$(IN_VENV)),yes ($$VIRTUAL_ENV),no)"
	@printf "  $(C_BOLD)root$(C_RESET):     %s\n" "$(if $(filter 0,$(IS_ROOT)),yes,no)"
	@printf "  $(C_BOLD)Package$(C_RESET):  %s v$(VERSION)\n" "$(PKG_NAME)"
	@printf "  $(C_BOLD)Binary$(C_RESET):   %s\n" "$$(command -v $(CMD) 2>/dev/null || echo 'NOT IN PATH')"
	@echo
	$(call say,Required commands)
	@for c in tar xz chown chmod find git; do \
		if command -v $$c >/dev/null 2>&1; then \
			printf "  $(C_GREEN)✔$(C_RESET) %s → %s\n" "$$c" "$$(command -v $$c)"; \
		else \
			printf "  $(C_RED)✘$(C_RESET) %s (missing)\n" "$$c"; \
		fi; \
	done
	@for c in mariadb mysql mariadb-dump mysqldump; do \
		if command -v $$c >/dev/null 2>&1; then \
			printf "  $(C_GREEN)✔$(C_RESET) %s → %s\n" "$$c" "$$(command -v $$c)"; \
		else \
			printf "  $(C_DIM)○$(C_RESET) %s (fallback)\n" "$$c"; \
		fi; \
	done
	@echo
	$(call say,Config file)
	@found=0; \
	for f in "$$HOME/.moodle_ninja_config.json" "/root/.moodle_ninja_config.json"; do \
		if [ -f "$$f" ]; then \
			printf "  $(C_GREEN)✔$(C_RESET) %s\n" "$$f"; found=1; \
		fi; \
	done; \
	[ "$$found" = "1" ] || printf "  $(C_YELLOW)⚠$(C_RESET) not yet created (will be on first run)\n"
	@echo
	$(call say,sudo secure_path)
	@if $(SUDO) -n true 2>/dev/null; then \
		SP=$$($(SUDO) -n sh -c 'echo $$PATH'); \
		printf "  PATH: %s\n" "$$SP"; \
		if echo ":$$SP:" | grep -q ":/usr/local/bin:"; then \
			printf "  $(C_GREEN)✔$(C_RESET) /usr/local/bin present\n"; \
		else \
			printf "  $(C_RED)✘$(C_RESET) /usr/local/bin MISSING — sudo won't find %s\n" "$(CMD)"; \
		fi; \
	else \
		printf "  $(C_YELLOW)⚠$(C_RESET) passwordless sudo unavailable\n"; \
	fi

# ─────────────────────────────────────────────────────────────────────
# Quality
# ─────────────────────────────────────────────────────────────────────
.PHONY: syntax
syntax: ## Fast syntax check (compileall)
	@$(PYTHON) -m compileall -q backup_server.py setup.py
	$(call ok,Syntax OK)

.PHONY: lint
lint: ## Lint (ruff if present, else flake8, else skip)
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check backup_server.py setup.py; \
	elif $(PYTHON) -c "import flake8" 2>/dev/null; then \
		$(PYTHON) -m flake8 backup_server.py setup.py; \
	else \
		printf "$(C_YELLOW)⚠$(C_RESET) no linter installed (pip install ruff)\n"; \
	fi

.PHONY: format
format: ## Format source (ruff format or black)
	@if command -v ruff >/dev/null 2>&1; then \
		ruff format backup_server.py setup.py; \
	elif $(PYTHON) -c "import black" 2>/dev/null; then \
		$(PYTHON) -m black backup_server.py setup.py; \
	else \
		printf "$(C_YELLOW)⚠$(C_RESET) no formatter (pip install ruff)\n"; \
	fi

.PHONY: test
test: syntax ## Run tests (pytest if installed, else syntax only)
	@set +e; \
	if $(PYTHON) -c "import pytest" 2>/dev/null; then \
		$(PYTHON) -m pytest -v; \
		rc=$$?; \
		if [ $$rc -eq 5 ]; then \
			printf "$(C_YELLOW)⚠$(C_RESET) no tests collected (pytest exit 5) — treating as pass\n"; \
		elif [ $$rc -ne 0 ]; then \
			exit $$rc; \
		fi; \
	else \
		printf "$(C_YELLOW)⚠$(C_RESET) pytest not installed — syntax only\n"; \
	fi

# ─────────────────────────────────────────────────────────────────────
# Build / clean / release
# ─────────────────────────────────────────────────────────────────────
.PHONY: build
build: clean-dist ## Build sdist + wheel into dist/
	@ver=$$(sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py | head -1); \
	printf "$(C_CYAN)▶$(C_RESET) Building distributions for v%s\n" "$$ver"
	@$(PIP) install $(PIP_FLAGS) $(PIP_QUIET) build
	@$(PYTHON) -m build
	$(call ok,Built:)
	@ls -lh dist/

.PHONY: clean-dist
clean-dist:
	@rm -rf dist/ build/ *.egg-info

.PHONY: clean
clean: clean-dist ## Remove caches, .pyc, build artifacts
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.py[co]' -delete
	@rm -rf .pytest_cache .ruff_cache .mypy_cache
	$(call ok,Cleaned)

.PHONY: version
version: ## Print current version (read from setup.py at run time)
	@sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py | head -1

.PHONY: bump-version
bump-version: ## Prompt to change version in setup.py (Enter = keep current)
	@current=$$(sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py | head -1); \
	if [ -z "$$current" ]; then \
		printf "$(C_RED)✘$(C_RESET) Could not read version from setup.py\n" >&2; exit 1; \
	fi; \
	\
	if [ -n "$(NEW_VERSION)" ]; then \
		new="$(NEW_VERSION)"; \
		printf "$(C_CYAN)▶$(C_RESET) Using NEW_VERSION from command line: %s\n" "$$new"; \
	else \
		read -p "New version [$$current]: " new; \
		new=$${new:-$$current}; \
	fi; \
	\
	if [ "$$new" = "$$current" ]; then \
		printf "$(C_GREEN)✔$(C_RESET) Keeping version %s\n" "$$current"; \
		exit 0; \
	fi; \
	\
	if ! echo "$$new" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		printf "$(C_RED)✘$(C_RESET) Invalid version '%s' — expected X.Y.Z\n" "$$new" >&2; exit 1; \
	fi; \
	\
	if git rev-parse "v$$new" >/dev/null 2>&1; then \
		printf "$(C_RED)✘$(C_RESET) Tag v%s already exists — pick a higher version\n" "$$new" >&2; exit 1; \
	fi; \
	\
	sed -i "s/version='[^']*'/version='$$new'/" setup.py; \
	git add setup.py; \
	git commit -q -m "chore: bump version to $$new"; \
	printf "$(C_GREEN)✔$(C_RESET) Bumped %s → %s (commit created)\n" "$$current" "$$new"

.PHONY: tag
tag: ## Create annotated git tag vX.Y.Z (reads version from setup.py)
	@ver=$$(sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py | head -1); \
	if [ -z "$$ver" ]; then \
		printf "$(C_RED)✘$(C_RESET) Could not read version from setup.py\n" >&2; exit 1; \
	fi; \
	if git rev-parse "v$$ver" >/dev/null 2>&1; then \
		printf "$(C_RED)✘$(C_RESET) tag v%s already exists\n" "$$ver" >&2; exit 1; \
	fi; \
	git tag -a "v$$ver" -m "Release v$$ver"; \
	printf "$(C_GREEN)✔$(C_RESET) Created tag v%s — push with: git push --tags\n" "$$ver"

.PHONY: release
release: test bump-version build tag ## Interactive: test → prompt version → build → tag → optional PyPI upload
	@ver=$$(sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py | head -1); \
	\
	printf "\n$(C_BOLD)$(C_CYAN)═══ Release v%s ready ═══$(C_RESET)\n\n" "$$ver"; \
	printf "$(C_BOLD)Built artifacts:$(C_RESET)\n"; \
	ls -lh dist/ 2>/dev/null | tail -n +2 | awk '{ printf "  %s  %s\n", $$5, $$9 }'; \
	printf "\n$(C_BOLD)Push to git:$(C_RESET)\n"; \
	printf "  git push\n"; \
	printf "  git push --tags\n\n"; \
	\
	if ! command -v twine >/dev/null 2>&1; then \
		printf "$(C_YELLOW)⚠$(C_RESET) twine not installed — PyPI upload unavailable.\n"; \
		printf "  Install: pip install twine\n"; \
		printf "  Then:    make upload\n"; \
		exit 0; \
	fi; \
	\
	read -p "Upload v$$ver to PyPI now? (y/N): " upload; \
	case "$$upload" in \
		y|Y|yes|YES) \
			printf "\n$(C_CYAN)▶$(C_RESET) Validating dist/ with twine check...\n"; \
			if ! twine check dist/*; then \
				printf "$(C_RED)✘$(C_RESET) twine check failed — fix before uploading\n" >&2; \
				exit 1; \
			fi; \
			printf "\n$(C_CYAN)▶$(C_RESET) Uploading to PyPI...\n"; \
			printf "$(C_DIM)  Username: __token__\n  Password: <your PyPI API token>$(C_RESET)\n\n"; \
			if twine upload dist/*; then \
				printf "\n$(C_GREEN)✔$(C_RESET) Released v%s to PyPI!\n" "$$ver"; \
				printf "  pip install --upgrade %s\n" "$(PKG_NAME)"; \
			else \
				printf "\n$(C_RED)✘$(C_RESET) Upload failed — retry with: twine upload dist/*\n" >&2; \
				exit 1; \
			fi; \
			;; \
		*) \
			printf "\n$(C_DIM)Upload skipped — when ready: make upload$(C_RESET)\n"; \
			;; \
	esac


.PHONY: upload
upload: check-dist ## Upload current dist/ to PyPI (requires twine; no rebuild)
	@if [ ! -d dist ] || [ -z "$$(ls -A dist 2>/dev/null)" ]; then \
		printf "$(C_RED)✘$(C_RESET) dist/ is empty — run 'make build' first\n" >&2; \
		exit 1; \
	fi
	@ver=$$(sed -n "s/.*version='\([^']*\)'.*/\1/p" setup.py | head -1); \
	printf "$(C_CYAN)▶$(C_RESET) Uploading v%s to PyPI\n" "$$ver"; \
	printf "$(C_DIM)  Username: __token__\n  Password: <your PyPI API token>$(C_RESET)\n\n"; \
	twine upload dist/*

.PHONY: check-dist
check-dist: ## Validate dist/ packages with twine check
	@if ! command -v twine >/dev/null 2>&1; then \
		printf "$(C_RED)✘$(C_RESET) twine not installed — pip install twine\n" >&2; \
		exit 1; \
	fi
	@if [ ! -d dist ] || [ -z "$$(ls -A dist 2>/dev/null)" ]; then \
		printf "$(C_RED)✘$(C_RESET) dist/ is empty — run 'make build' first\n" >&2; \
		exit 1; \
	fi
	@twine check dist/*

# ─────────────────────────────────────────────────────────────────────
# Run without going through the console script
# ─────────────────────────────────────────────────────────────────────
.PHONY: run
run: ## Run the menu directly (bypasses console script)
	$(SUDO) $(PYTHON) backup_server.py

.PHONY: run-cli
run-cli: ## Run CLI directly — pass ARGS="backup -v"
	@test -n "$(ARGS)" || { \
		printf "$(C_RED)✘$(C_RESET) Usage: make run-cli ARGS=\"backup -v\"\n" >&2; \
		exit 1; \
	}
	$(SUDO) $(PYTHON) backup_server.py $(ARGS)

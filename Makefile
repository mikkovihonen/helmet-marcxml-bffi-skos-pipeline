.PHONY: help test lint format \
        observability-up observability-down observability-reset-prometheus \
        install-hooks

# Auto-install the pre-commit hook the first time `make` runs in this clone.
_ := $(shell test -d .git -a ! -f .git/.hookspath-set \
        && git config core.hooksPath .githooks 2>/dev/null \
        && touch .git/.hookspath-set \
        && echo "[Makefile] installed pre-commit hook: .githooks/pre-commit" >&2)

# Container runtime detection. macOS operators often use podman with a
# shell alias `docker -> podman` (zsh-side, not visible to make's
# /bin/sh); Linux operators usually have a real `docker` binary.
DOCKER ?= $(shell command -v docker 2>/dev/null || command -v podman 2>/dev/null)
ifeq ($(strip $(DOCKER)),)
$(error Neither docker nor podman found in PATH. Install one or set DOCKER=<path> on the make invocation.)
endif

help:
	@echo "Targets:"
	@echo "  test               - run pytest"
	@echo "  lint               - ruff check + ruff format --check + mypy --strict"
	@echo "  format             - ruff format + ruff check --fix"
	@echo "  observability-up   - start local Prometheus + Grafana + Caddy at localhost:8080"
	@echo "  observability-down - stop the observability stack"
	@echo "  observability-reset-prometheus - wipe all series from Prometheus TSDB"
	@echo "  install-hooks      - force re-install of .githooks/ as the git hookspath"

test:
	uv run pytest tests/

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy --strict src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# Local observability stack: Prometheus + Grafana + Caddy in the
# `observability` Docker Compose profile, plus the host-side
# `bffi-pipeline serve-metrics` exporter that tails
# `runs/*/stage-events.jsonl` sidecars. Single entry point: localhost:8080.
observability-up:
	$(DOCKER) compose --profile observability up -d prometheus grafana caddy
	@if pgrep -f 'bffi-pipeline serve-metrics' >/dev/null; then \
	  echo "serve-metrics already running (PID $$(pgrep -f 'bffi-pipeline serve-metrics' | head -1)); leaving it alone." ; \
	else \
	  mkdir -p runs ; \
	  nohup env BFFI_OBSERVABILITY_SIDECAR=none uv run bffi-pipeline serve-metrics \
	      --port 9100 \
	      --watch-glob 'runs/*/stage-events.jsonl' \
	    > /tmp/bffi-exporter.log 2>&1 & \
	  sleep 1 ; \
	  if pgrep -f 'bffi-pipeline serve-metrics' >/dev/null; then \
	    echo "Started serve-metrics (PID $$(pgrep -f 'bffi-pipeline serve-metrics' | head -1)); log → /tmp/bffi-exporter.log" ; \
	  else \
	    echo "WARNING: serve-metrics failed to start; see /tmp/bffi-exporter.log" >&2 ; \
	  fi ; \
	fi
	@echo
	@echo "Single entry point (Caddy reverse-proxy):  http://localhost:8080"
	@echo "  /grafana/     - anonymous Viewer; bundled bffi-pipeline dashboard"
	@echo "  /prometheus/  - ad-hoc PromQL"
	@echo "  /files/       - browse runs/ (per-record artifacts, diff TSVs)"

observability-down:
	$(DOCKER) compose --profile observability stop caddy grafana prometheus
	@if pgrep -f 'bffi-pipeline serve-metrics' >/dev/null; then \
	  pkill -TERM -f 'bffi-pipeline serve-metrics' && \
	  echo "Stopped serve-metrics" ; \
	else \
	  echo "serve-metrics not running" ; \
	fi

observability-reset-prometheus:
	@curl -fsS -X POST 'http://localhost:8080/prometheus/api/v1/admin/tsdb/delete_series?match[]=%7B__name__%3D~%22.%2B%22%7D' >/dev/null && \
	  curl -fsS -X POST 'http://localhost:8080/prometheus/api/v1/admin/tsdb/clean_tombstones' >/dev/null && \
	  echo "Prometheus TSDB wiped (all series + tombstones cleaned)." && \
	  if pgrep -f 'bffi-pipeline serve-metrics' >/dev/null; then \
	    echo ; \
	    echo "Note: a live serve-metrics exporter is running — it will repopulate the TSDB"; \
	    echo "on the next ~5 s scrape. Kill it with:"; \
	    echo "  pkill -f 'bffi-pipeline serve-metrics'"; \
	  fi

# Manual re-install path for the pre-commit hook.
install-hooks:
	@git config core.hooksPath .githooks
	@mkdir -p .git && touch .git/.hookspath-set
	@echo "core.hooksPath = $$(git config core.hooksPath)"
	@echo "Pre-commit hook installed; commits touching *.py will now run"
	@echo "  make lint && make test"
	@echo "before completing. Bypass with --no-verify."

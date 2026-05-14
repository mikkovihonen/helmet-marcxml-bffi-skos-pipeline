.PHONY: help convert eval publish refresh-finto test test-integration lint format \
        observability-up observability-down observability-reset-prometheus clean-caches \
        install-hooks

# Auto-install the pre-commit hook the first time `make` runs in this clone.
# Evaluated eagerly at Makefile parse-time, so any target invocation
# triggers it. Gated on (a) being inside a git checkout, and (b) a
# sentinel file under .git/ so we only run `git config` once per clone
# (the operator can re-run via `make install-hooks` if they ever blow
# away the sentinel). Stderr is suppressed so a non-git extract of the
# repo doesn't print a confusing error on `make help`.
_ := $(shell test -d .git -a ! -f .git/.hookspath-set \
        && git config core.hooksPath .githooks 2>/dev/null \
        && touch .git/.hookspath-set \
        && echo "[Makefile] installed pre-commit hook: .githooks/pre-commit" >&2)

# Container runtime detection. macOS operators often use podman with a
# shell alias `docker -> podman` (zsh-side, not visible to make's
# /bin/sh); Linux operators usually have a real `docker` binary.
# Prefer docker if present, fall back to podman. Override on the CLI
# with `make DOCKER=docker observability-up` if the auto-pick is wrong.
DOCKER ?= $(shell command -v docker 2>/dev/null || command -v podman 2>/dev/null)
ifeq ($(strip $(DOCKER)),)
$(error Neither docker nor podman found in PATH. Install one or set DOCKER=<path> on the make invocation.)
endif

help:
	@echo "Targets:"
	@echo "  eval LABEL=<id>    - run gold-set evaluation locally on M5 Max (M12)"
	@echo "  refresh-finto      - download Finto vocab dumps + load into Fuseki (M11 3b)"
	@echo "  test               - run pytest (excluding requires_llm)"
	@echo "  test-integration   - run integration tests (Docker stack required)"
	@echo "  lint               - ruff check + ruff format --check + mypy --strict"
	@echo "  format             - ruff format + ruff check --fix"
	@echo "  observability-up   - start local Prometheus + Grafana (P-11 Phase D)"
	@echo "  observability-down - stop the observability stack"
	@echo "  observability-reset-prometheus - wipe all series from Prometheus TSDB"
	@echo "  clean-caches       - remove the M6 judge and M9 picker decision caches"
	@echo "  install-hooks      - force re-install of .githooks/ as the git hookspath"

eval:
	@if [ -z "$(LABEL)" ]; then \
		echo "Usage: make eval LABEL=<run-label>"; \
		echo "  e.g. make eval LABEL=qwen3-32b-prompt-v3"; \
		exit 2; \
	fi
	uv run bffi-pipeline eval --run-label "$(LABEL)"

refresh-finto:
	uv run bffi-pipeline load-finto --force

test:
	uv run pytest tests/ -m "not requires_llm"

test-integration:
	@echo "not implemented"

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy --strict src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# P-11 Phase D local observability stack. The two services use the
# ``observability`` Docker Compose profile so they're excluded from
# the default ``docker compose up`` and only fire when an operator
# explicitly opts in. Pair with ``bffi-pipeline serve-metrics`` on
# the host (the exporter Prometheus scrapes).
observability-up:
	$(DOCKER) compose --profile observability up -d prometheus grafana caddy
	@if pgrep -f 'bffi-pipeline serve-metrics' >/dev/null; then \
	  echo "serve-metrics already running (PID $$(pgrep -f 'bffi-pipeline serve-metrics' | head -1)); leaving it alone." ; \
	else \
	  mkdir -p runs ; \
	  nohup uv run bffi-pipeline serve-metrics \
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
	@echo "  /files/       - browse runs/ (cataloguer TSVs, export tarballs, per-record artifacts)"

observability-down:
	$(DOCKER) compose --profile observability stop caddy grafana prometheus
	@if pgrep -f 'bffi-pipeline serve-metrics' >/dev/null; then \
	  pkill -TERM -f 'bffi-pipeline serve-metrics' && \
	  echo "Stopped serve-metrics" ; \
	else \
	  echo "serve-metrics not running" ; \
	fi

# Wipe every series + tombstone from the Prometheus TSDB without
# restarting the container. Uses the admin API (``--web.enable-admin-api``
# is on by default in the bundled compose stack).
#
# Caveats:
#   1. A running ``bffi-pipeline serve-metrics`` exporter holds an
#      in-memory Prometheus registry and re-emits every series on the
#      next 5 s scrape. To actually see an empty dashboard, ``pkill -f
#      'bffi-pipeline serve-metrics'`` first (or wait for the next
#      ``bffi-pipeline run`` to spawn + reap a fresh exporter via the
#      runner lifecycle).
#   2. For per-run cleanup instead, use ``bffi-pipeline runs prune
#      --apply --reset-prometheus``.
#   3. For a full volume wipe (WAL on disk gone too), stop the
#      container + ``docker volume rm
#      helmet-marcxml-bffi-skos-pipeline_prometheus-data`` instead.
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

# Manual re-install path for the pre-commit hook. The auto-install at
# the top of this Makefile fires once per clone; this target re-runs
# it unconditionally so an operator who blew away the sentinel
# (`rm .git/.hookspath-set`) or wants to verify the config can do so
# explicitly.
install-hooks:
	@git config core.hooksPath .githooks
	@mkdir -p .git && touch .git/.hookspath-set
	@echo "core.hooksPath = $$(git config core.hooksPath)"
	@echo "Pre-commit hook installed; commits touching *.py will now run"
	@echo "  make lint && make test"
	@echo "before completing. Bypass with --no-verify."

# P-10 Phase B: remove the M6 judge-pair and M9 picker-decision caches.
# Cache files are gitignored; this is the operator-side reset knob for
# benching cold-cache scenarios or recovering from a corrupted SQLite
# file. Safe to run at any time — the next pipeline invocation
# rebuilds the schema lazily on first miss.
clean-caches:
	@DATA_DIR=$$(uv run python -c 'from bffi_pipeline.config import get_settings; print(get_settings().data_dir)') ; \
	  for f in judge-cache.sqlite reconcile-cache.sqlite ; do \
	    if [ -f "$$DATA_DIR/$$f" ]; then \
	      echo "removing $$DATA_DIR/$$f" ; \
	      rm -f "$$DATA_DIR/$$f" ; \
	    else \
	      echo "$$DATA_DIR/$$f not present (already clean)" ; \
	    fi ; \
	  done

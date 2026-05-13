.PHONY: help convert eval publish refresh-finto test test-integration lint format \
        observability-up observability-down clean-caches

help:
	@echo "Targets:"
	@echo "  convert FILE=...   - run MARCXML to BFFI on a directory (M2-M10)"
	@echo "  eval LABEL=<id>    - run gold-set evaluation locally on M5 Max (M12)"
	@echo "  publish            - load to Fuseki and refresh Skosmos (M10)"
	@echo "  refresh-finto      - download Finto vocab dumps + load into Fuseki (M11 3b)"
	@echo "  test               - run pytest (excluding requires_llm)"
	@echo "  test-integration   - run integration tests (Docker stack required)"
	@echo "  lint               - ruff check + ruff format --check + mypy --strict"
	@echo "  format             - ruff format + ruff check --fix"
	@echo "  observability-up   - start local Prometheus + Grafana (P-11 Phase D)"
	@echo "  observability-down - stop the observability stack"
	@echo "  clean-caches       - remove the M6 judge and M9 picker decision caches"

convert:
	@echo "not implemented"

eval:
	@if [ -z "$(LABEL)" ]; then \
		echo "Usage: make eval LABEL=<run-label>"; \
		echo "  e.g. make eval LABEL=qwen3-32b-prompt-v3"; \
		exit 2; \
	fi
	uv run bffi-pipeline eval --run-label "$(LABEL)"

publish:
	@echo "not implemented"

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
	docker compose --profile observability up -d prometheus grafana
	@echo
	@echo "Prometheus:  http://localhost:9091"
	@echo "Grafana:     http://localhost:3001  (anonymous Viewer; bundled bffi-pipeline dashboard)"
	@echo
	@echo "Run the exporter alongside the pipeline:"
	@echo "  uv run bffi-pipeline serve-metrics"

observability-down:
	docker compose --profile observability stop prometheus grafana

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

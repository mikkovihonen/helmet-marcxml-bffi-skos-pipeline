.PHONY: help convert eval publish test test-integration lint format

help:
	@echo "Targets:"
	@echo "  convert FILE=...   - run MARCXML to BFFI on a directory (M2-M10)"
	@echo "  eval LABEL=<id>    - run gold-set evaluation locally on M5 Max (M12)"
	@echo "  publish            - load to Fuseki and refresh Skosmos (M10)"
	@echo "  test               - run pytest (excluding requires_llm)"
	@echo "  test-integration   - run integration tests (Docker stack required)"
	@echo "  lint               - ruff check + ruff format --check + mypy --strict"
	@echo "  format             - ruff format + ruff check --fix"

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

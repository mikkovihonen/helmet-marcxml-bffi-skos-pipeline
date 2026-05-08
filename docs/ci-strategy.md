# CI strategy

CI runs on **GitHub-hosted Linux runners only**. The LLM eval is **not** in CI — local LLM dependency makes it impractical on hosted runners and self-hosted M5 Max introduces operational/security overhead unjustified for a solo project.

Quality gates that involve the LLM run manually on the M5 Max via `make eval`, with results pasted into the PR description per the PR template below.

## What runs in CI

| Check | Runner | Trigger |
|---|---|---|
| `ruff check` | ubuntu-24.04 | every push, every PR |
| `ruff format --check` | ubuntu-24.04 | every push, every PR |
| `mypy --strict src` | ubuntu-24.04 | every push, every PR |
| Unit tests (`pytest tests/unit`) | ubuntu-24.04 | every push, every PR |
| Integration tests (Fuseki/Skosmos via Docker services), excluding `requires_llm` | ubuntu-24.04 | every push, every PR |

## What runs manually

| Check | Where | Trigger |
|---|---|---|
| `make eval` against gold set hold-out | M5 Max, locally | before any PR touching `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/` |
| `make test-integration` (incl. `requires_llm` mark) | M5 Max with Ollama running | on demand |

## Workflow file

`.github/workflows/ci.yml`:

```yaml
name: ci
on: [push, pull_request]
jobs:
  lint-and-test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with: { submodules: recursive }
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run ruff check src tests
      - run: uv run ruff format --check src tests
      - run: uv run mypy --strict src
      - run: uv run pytest tests/unit -v

  integration:
    runs-on: ubuntu-24.04
    services:
      fuseki:
        image: stain/jena-fuseki:5.0.0     # pinned per M10
        ports: ["3030:3030"]
        env:
          ADMIN_PASSWORD: admin
          FUSEKI_DATASET_1: bffi
    steps:
      - uses: actions/checkout@v4
        with: { submodules: recursive }
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run pytest tests/integration -v -m "not requires_llm"
```

## PR template

`.github/pull_request_template.md`:

```markdown
## Summary
<!-- what does this PR do -->

## Eval results (required if touching prompts, gold, or judge)
<!-- paste output of `make eval` here, or write "N/A" with brief justification -->

## Checklist
- [ ] `make lint && make test` pass locally
- [ ] If this PR touches the judge, prompts, or gold set, eval results above
- [ ] Documentation updated (README / runbook / docstrings) if user-facing surface changed
```

## Why not self-hosted runner

A self-hosted runner on the M5 Max would let CI run the LLM eval automatically, but:

- The runner can be triggered by anyone with write access to the repo (security risk if the project later accepts external contributions).
- The laptop has to be online and idle when CI fires.
- Self-hosted runners on personal machines are a category of operational complexity worth avoiding when you can.

If the project later sees regular external contributors and the eval discipline starts slipping, revisit this decision. For now, the manual-eval-with-PR-description approach matches NLF's own GitHub repos — most of them have lightweight CI focused on lint and unit tests rather than full system evaluation.

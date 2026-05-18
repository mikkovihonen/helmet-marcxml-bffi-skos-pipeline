# P-40 — Move to Python 3.14 and lift dependency floors to current latest

**Status**: in-progress.
**Source proposal**: drafted 2026-05-18 in the same multi-commit sequence that lands this plan; the proposal was not committed independently before graduation, so there is no separate `proposed/p-40-…` commit to recover via `git show`. The proposal-shape draft's reasoning is preserved verbatim in the Motivation / Approach / Open-questions content this plan rewrites — `git log --follow docs/plans/in-progress/p-40-python-314-and-dependency-uplift.md` starts at the graduation rename. The plan was also never landed in `backlog/` independently — it transitions straight to `in-progress/` in the same commit that lands Phase A.
**Plan-base commit**: `63bd5b3` (`docs/plans/p-38: graduate to completed (Phase D shipped across M2-M9)`). Drift check before executing:
`git diff 63bd5b3..HEAD -- pyproject.toml uv.lock .github/workflows/ci.yml Makefile .python-version docs/tech-stack.md`.
**Phase commits**:

- Phase A (Python 3.14 toolchain): `ebf6c11`
- Phase B (dependency floor lift): `2604ce0`
- Phase C.1 (mypy 1 → 2): `bca79dc`
- Phase C.2 (pytest 8 → 9 + pytest-asyncio 0 → 1): `<unfilled>` (this commit)
- Phase C.3 (langchain-openai 0 → 1): `<unfilled>`
- Phase C.4 (sentence-transformers 3 → 5): `<unfilled>`
- Phase C.5 (lxml 5 → 6): `<unfilled>`
- Phase C.6 (python-ulid 2 → 3): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: ~1 day for Phase A; ~half a day for Phase B; ~0.5-1 day for Phase C, dominated by the mypy 2.x audit and the langchain-openai 1.x call-site rewrite. LLM eval (`make eval`) runs locally on the M5 Max per `CLAUDE.md` § "Workflow rules" — never in CI.

## Goal

Align `pyproject.toml`'s stated contract with what `uv.lock` already resolves, and lift `requires-python` to 3.14 so the declared toolchain reflects the interpreter the project actually targets. After this plan ships:

1. `requires-python = ">=3.14"`, `tool.ruff.target-version = "py314"`, `tool.mypy.python_version = "3.14"`, and a new `.python-version` file all agree on 3.14.
2. Every dependency floor in `pyproject.toml` is `>=<X.Y>` (or `>=<X>` for the seven majors crossed) such that a future `uv lock --upgrade` cannot regress through a major-version delta without an explicit `pyproject.toml` edit.
3. CI passes on ubuntu-24.04 with `uv sync --frozen` against the regenerated lockfile.
4. `make lint && make test` green; `mypy --strict src` green against mypy 2.x.
5. Each breaking-major absorption is its own commit so a future `git bisect` can isolate a regression to one upgrade class.

**Out of scope** (deliberate): free-threaded Python (`python3.14t`, PEP 703). That requires a separately-built interpreter, has materially thinner cp314t wheel coverage to re-verify, and has no current consumer in this codebase. It is the enabling prerequisite for any future in-stage multithreading work and warrants its own proposal (call it P-41 when drafted) sequenced after P-40; P-40 is the precondition that gets us onto 3.14 in the first place.

## Definition of done

- `pyproject.toml`: `requires-python`, `tool.ruff.target-version`, `tool.mypy.python_version`, every dependency floor updated; `tool.pytest_asyncio.asyncio_mode = "strict"` set explicitly (the pytest-asyncio 1.x default differs from 0.x and the implicit-mode resolution is itself the footgun being closed).
- `.python-version` exists at the repo root containing `3.14`.
- `uv.lock` regenerated; `uv sync --frozen` reproduces the install set on both macOS arm64 and ubuntu-24.04.
- `.github/workflows/ci.yml` unchanged (verified, not just assumed) — `astral-sh/setup-uv@v3` installs the interpreter the lockfile points at, so no workflow edit is required.
- `mypy --strict src` zero errors against mypy 2.x.
- `pytest tests/unit` and `pytest tests/integration -m "not requires_llm"` both green on the new lockfile.
- `docs/tech-stack.md`'s "Python" reference updated in Phase A's commit.
- One commit per breakage class in Phase C (six commits in total under Phase C).
- M5 + M6 smoke run on the curated dev sample (the 13 cataloguer-pinned bib IDs, per memory `curated_dev_sample.md`) produces byte-equal RDF/MARCXML output vs the pre-plan baseline on Phases A + B; Phase C.3 / C.4 / C.5 each carry their own targeted verification (see per-phase acceptance).

## Current state

As of plan-base commit `63bd5b3`:

- `pyproject.toml:7` declares `requires-python = ">=3.11,<3.13"`.
- `pyproject.toml:65` sets `tool.ruff.target-version = "py311"`.
- `pyproject.toml:112` sets `tool.mypy.python_version = "3.11"`.
- No `.python-version` file exists at the repo root.
- `uv.lock` resolves the 21 runtime + 6 dev dependencies cleanly; key versions: `mypy 2.0.0`, `pytest 9.0.3`, `pytest-asyncio 1.3.0`, `langchain-openai 1.2.1`, `sentence-transformers 5.4.1`, `lxml 6.1.0`, `python-ulid 3.1.0`, `numpy 2.4.4`, `faiss-cpu 1.13.2`, `asyncpg 0.31.0`, `pydantic 2.13.4`. None of these are reflected in the `pyproject.toml` floors — every floor in the file is satisfied by versions ≥ 6 months stale relative to `uv.lock`.
- `.github/workflows/ci.yml` uses `astral-sh/setup-uv@v3` + `uv sync --frozen`; there is no `actions/setup-python` step and no Python-version matrix.
- `tool.mypy.overrides` silences `faiss`, `sentence_transformers`, `skosify`, `pyshacl`, `SPARQLWrapper`, `pymarc` modules (all `ignore_missing_imports = true`) plus a `no-untyped-call` disable on `marcxml_export_pipeline.sierra.marcxml`.
- The repo's typing surface — 108 hits for `TypeAlias`/`TypeGuard`/`TypeVar`/`ParamSpec`/`Self`/`from __future__` across `src/**/*.py` — is non-trivial; mypy 2.x's tightened generic-default / Self-in-protocol inference is the largest unknown in Phase C.1.
- Async surface is minimal: one async test (per `grep -rln "async def" src/ | wc -l`) plus the `marcxml_export_pipeline.sierra` SQLAlchemy/asyncpg pipeline. `pyproject.toml` does not set `tool.pytest_asyncio.asyncio_mode` — implicit on pytest-asyncio 0.x, footgun on 1.x.

---

## Phase A — Python 3.14 toolchain bump

Estimated wall-time: ~1 day, dominated by the first wheel-cache hydration on a clean uv runner and the M5/M6 smoke verification on the dev sample.

### A1. Verify cp314 wheel coverage on the target platforms

```bash
# From the repo root, with no `requires-python` change applied yet.
uv pip compile --python 3.14 pyproject.toml -o /tmp/p40-cp314-check.txt
```

Then sanity-grep for sdist-only or pre-3.14 wheel resolutions on the native deps:

```bash
grep -E "^(faiss-cpu|lxml|numpy|torch|asyncpg|sentence-transformers|pymarc)\b" /tmp/p40-cp314-check.txt
```

**Verification**: every native dep resolves to a `cp314-` (or `abi3` / `py3-none-any`) wheel on both `macosx_11_0_arm64` (operator M5 Max) and `manylinux*_x86_64` (CI ubuntu-24.04). If any resolves to sdist-only on either platform, stop and surface to the Open-issues section before continuing — the live options at that point are: stay on 3.13 (still a win), build the offender from source via Homebrew/apt, or defer P-40 until upstream ships the wheel.

### A2. Edit `pyproject.toml` and create `.python-version`

```toml
# pyproject.toml diff
requires-python = ">=3.14"      # was: ">=3.11,<3.13"

[tool.ruff]
target-version = "py314"        # was: "py311"

[tool.mypy]
python_version = "3.14"         # was: "3.11"
```

Write `<repo-root>/.python-version` with the single line `3.14`.

### A3. Regenerate `uv.lock`

```bash
uv lock          # NOT --upgrade — Phase A only widens the interpreter floor; dep floors stay on Phase B's commit
git diff uv.lock # expect mostly resolver-metadata churn; package versions should not move
```

**Verification**: every `name = "<dep>"` block's `version = "..."` stays identical to plan-base; only `requires-python` references and possibly `markers` lines change. If a dep version moves in Phase A, that is a deferred upgrade leaking from a stale resolver hint — investigate before merging.

### A4. Run the local lint + test suite

```bash
make lint
make test
uv run pytest tests/integration -m "not requires_llm"
```

**Verification**: all green. If `mypy --strict` produces new 3.14-grammar complaints (e.g. `type` statement, `TypeAlias` warnings on PEP 695 alternatives), absorb the fix into Phase A's commit — these are syntax-shape issues, not the substantive mypy 2.x absorption (which is Phase C.1).

### A5. Curated dev-sample byte-equality smoke

Run the dev-sample pipeline end-to-end and compare the produced RDF/MARCXML against a pre-plan baseline captured at the plan-base commit:

```bash
# Pre-plan baseline (captured separately at plan-base commit).
git stash               # park Phase A changes
uv run bffi-pipeline run-stages M2,M3 --sample-dir <dev-sample-dir> --output-dir /tmp/p40-baseline
git stash pop
uv run bffi-pipeline run-stages M2,M3 --sample-dir <dev-sample-dir> --output-dir /tmp/p40-phaseA
diff -r /tmp/p40-baseline /tmp/p40-phaseA
```

**Verification**: zero diff. The 13-record dev sample (memory `curated_dev_sample.md`) is intentionally small enough that bytewise diffing is tractable. M5/M6/M8/M9 are excluded from Phase A's smoke because they exercise the LLM/embedding stack, which Phase C absorbs separately.

### A6. Update `docs/tech-stack.md`

Edit the "Python" / "Toolchain" line(s) in `docs/tech-stack.md` to read `Python 3.14` (or whatever the project's existing phrasing convention is at execution time). One-line edit; lands in the same commit as A2-A5.

### A7. Phase A acceptance

- [ ] cp314 wheel coverage confirmed for every native dep on both target platforms (A1).
- [ ] `pyproject.toml` + `.python-version` written (A2).
- [ ] `uv.lock` regenerated with no dep-version moves (A3).
- [ ] `make lint && make test` + integration tests green (A4).
- [ ] Dev-sample byte-equality smoke clean (A5).
- [ ] `docs/tech-stack.md` updated (A6).
- [ ] CI green on a feature-branch push before merge (final pre-merge check).

### A8. Rollback

`git revert <Phase-A-commit>`. The plan-base lockfile is recovered atomically; the `.python-version` file is deleted by the revert. No data-format changes, no migration cost. If revert lands after Phases B/C have started, the upstream phases revert too (Phase B/C floors depend on Phase A's toolchain). Bisect-friendly because Phase A is a single commit.

---

## Phase B — Lift dependency floors to track `uv.lock`

Estimated wall-time: ~half a day. Mechanical edit; the time is in the diff review and the post-edit CI run.

### B1. Edit `pyproject.toml` dependency floors

Rewrite each line in `[project]`'s `dependencies` and `[dependency-groups]`'s `dev` block per the policy: for packages whose major version did **not** change between current floor and `uv.lock`'s resolved version, bump to `>=<resolved-major.minor>`; for packages whose major version **did** change, pin to `>=<resolved-major.0>`. Concretely (against the `uv.lock` state at plan-base — re-derive at execution time if the lockfile has moved):

```toml
# [project] dependencies
"typer>=0.25",
"pydantic>=2.13",
"pydantic-settings>=2.14",
"python-dotenv>=1.2",
"rdflib>=7.6",
"pymarc>=5.3",
"lxml>=6",                          # major bump (5 → 6)
"pyshacl>=0.31",
"skosify>=2.3",                     # unchanged
"SPARQLWrapper>=2.0",               # unchanged
"langchain-openai>=1",              # major bump (0 → 1)
"sentence-transformers>=5",         # major bump (3 → 5)
"faiss-cpu>=1.13",
"python-ulid>=3",                   # major bump (2 → 3)
"jinja2>=3.1",                      # unchanged at .minor
"httpx>=0.28",
"requests>=2.33",
"lingua-language-detector>=2.2",
"prometheus-client>=0.25",
"sqlalchemy[asyncio]>=2.0",
"asyncpg>=0.31",

# [dependency-groups] dev
"pytest>=9",                        # major bump (8 → 9)
"pytest-asyncio>=1",                # major bump (0 → 1)
"ruff>=0.15",
"mypy>=2",                          # major bump (1 → 2)
"types-requests",                   # version-less, unchanged
"lxml-stubs",                       # version-less, unchanged
```

Floor-only convention preserved — no upper bounds added on any dep (consistent with the existing 21 entries).

### B2. Regenerate `uv.lock`

```bash
uv lock
git diff uv.lock
```

**Verification**: diff should be empty or near-empty for every package whose Phase B floor was already satisfied by the lockfile (every row in B1's table). If any dep's resolution moves in Phase B, it means the previous floor was being honoured by a deliberately-pinned older version somewhere in the resolver graph — investigate before merging.

### B3. Run the local lint + test suite

```bash
make lint
make test
uv run pytest tests/integration -m "not requires_llm"
```

**Verification**: all green. No behaviour change is expected at this phase — the lockfile is identical, only the floors moved. Any test failure at B3 is evidence of a Phase B floor edit error (e.g. accidentally specifying `>=<resolved.patch>` instead of `>=<resolved.minor>`).

### B4. Phase B acceptance

- [ ] `pyproject.toml` floors edited per B1 (21 runtime + 6 dev entries).
- [ ] `uv.lock` regenerated with no package-version moves (B2).
- [ ] `make lint && make test` + integration tests green (B3).
- [ ] CI green on the feature branch.

### B5. Rollback

`git revert <Phase-B-commit>`. Lockfile is unaffected (B2's regen was a no-op). Zero data-format consequences.

---

## Phase C — Absorb breaking-major changes, one commit per breakage class

Estimated wall-time: ~0.5-1 day total across the six sub-phases. Sequenced by risk: cheapest and most contained first; the langchain-openai 1.x audit last because it has the largest behavioural surface and may compose with proposal P-06.

Each sub-phase ships as its own commit. If a sub-phase blocks (e.g. C.3's API audit reveals a non-trivial rewrite), the preceding sub-phases ship anyway — the breaking-major absorption is monotonic.

### C.1 — mypy 1 → 2

- `uv run mypy --strict src` against the Phase-B lockfile. Triage every new finding:
  - Generic-default inference tightening → fix at the call site (preferred) or add a `# type: ignore[<code>]` with a brief reason comment if the new finding is a known mypy 2.x false positive documented in mypy's changelog.
  - `Self` / `TypeGuard` / `TypeIs` ambiguity → adopt the 2.x-preferred form per mypy's migration guide.
- Do **not** expand `tool.mypy.overrides` to silence new findings in `src/bffi_pipeline/` proper. The existing third-party silences (`faiss`, `sentence_transformers`, `skosify`, `pyshacl`, `SPARQLWrapper`, `pymarc`) plus the `marcxml_export_pipeline.sierra.marcxml` `no-untyped-call` disable may absorb new 2.x complaints if they surface there — that is in scope.

**C.1 verification**: `mypy --strict src` zero errors; `make test` still green; `git diff src/` shows only typing-related edits (no semantic logic changes).

**C.1 rollback**: revert the C.1 commit. Phase B's `mypy>=2` floor would then conflict with mypy 1.x — so either follow with a `mypy>=1.10,<2` floor edit (one-line restore in `pyproject.toml`) or revert Phase B as well.

### C.2 — pytest 8 → 9 + pytest-asyncio 0 → 1

- Add `[tool.pytest_asyncio]` section with `asyncio_mode = "strict"` to `pyproject.toml`. This is the single most consequential edit in C.2 — pytest-asyncio 1.x's implicit default differs from 0.x and "strict" makes the project's async-test contract explicit and re-readable.
- Triage any pytest 9.x deprecation removals that surface (notably `pytest.warns(None)` patterns in the test suite — grep first: `git grep -nE "pytest\.warns\(\s*None"`).
- Re-run `pytest tests/unit -v` and `pytest tests/integration -m "not requires_llm" -v`; verify the one existing async test in the repo (per Current State) is collected with the expected marker behaviour.

**C.2 verification**: all unit + integration tests green; pytest output shows no deprecation warnings; async test is collected and runs.

**C.2 rollback**: revert the C.2 commit. As with C.1, this conflicts with Phase B's floors (`pytest>=9`, `pytest-asyncio>=1`); follow up with a `<` upper bound or revert Phase B.

### C.3 — langchain-openai 0 → 1

Largest behavioural surface in Phase C. The pipeline's call sites for the langchain client are concentrated in:

- `src/bffi_pipeline/stages/m6/clients.py`
- `src/bffi_pipeline/stages/m6/runner.py`
- `src/bffi_pipeline/stages/m6/batch.py`
- `src/bffi_pipeline/stages/m9/picker_chain.py`
- `src/bffi_pipeline/stages/m9/requests.py`

Audit checklist (do, in order):

1. Read each file and enumerate the `ChatOpenAI` / `LangChain*` API surfaces in use: constructor args, callback handlers, `bind_tools` / `with_structured_output`, response-format / `JsonOutputFunctionsParser` plumbing.
2. Cross-reference each against the langchain-openai 1.x changelog and migration guide. Identify deltas.
3. Rewrite call sites for the 1.x API. Prefer the smallest mechanical diff; do not bundle stylistic refactors.
4. Re-run the M6 LLM judge against the curated dev sample (memory `curated_dev_sample.md`) — operator-only, on the M5 Max, with the mlx-lm sidecar running. Compare judge verdicts to a pre-plan baseline captured at the Plan-base commit. **A verdict-distribution drift here is the canary that catches structured-output regressions** that smoke tests would not.
5. If `with_structured_output` semantics shift between 0.x and 1.x, cross-reference proposal `p-06-structured-output-backend.md` — Phase C.3 may surface evidence relevant to P-06's open questions even though P-06 is gated separately.

**C.3 verification**: M6 LLM-judge verdicts on the curated dev sample match the pre-plan baseline to within the existing judge-cache deterministic-replay tolerance (i.e. zero deltas when the cache is warm; otherwise the same verdicts within the previously-measured cache-miss variance). `make lint && make test` green.

**C.3 rollback**: revert the C.3 commit. As with C.1/C.2, this conflicts with Phase B's `langchain-openai>=1` floor; restore the floor to `>=0.1,<1` or revert Phase B.

### C.4 — sentence-transformers 3 → 5

- M5 is the sole consumer (`src/bffi_pipeline/stages/m5/embed.py`, `build.py`). M5 runs as a subprocess (memory `m5_process_isolation.md`), so MPS / FAISS memory regressions are catchable on a single 20 k-record bench without contaminating the M6 process.
- Run a 20 k-record M5 embed on the M5 Max. Capture: wall time, peak MPS memory, FAISS index size. Compare to the most recent pre-plan M5 bench in `docs/performance/` (or run a fresh baseline at Plan-base if none on file).

**C.4 verification**: wall time within ±15 % of baseline; peak MPS memory not materially higher; FAISS index size byte-equal (the embedding model itself is unchanged — `BAAI/bge-m3` per P-04 — only the wrapping library moved, so the embeddings themselves should be bit-identical or near-identical depending on whether sentence-transformers 5.x changed any normalisation defaults).

**C.4 rollback**: revert the C.4 commit. Conflicts with Phase B's `sentence-transformers>=5` floor; restore floor or revert B.

### C.5 — lxml 5 → 6

- Re-run M0/M1/M2 on the curated dev sample. lxml 6.x tightened parser-flag validation and dropped some Py2-era helpers — `src/bffi_pipeline/validation/marcxml.py` is the primary consumer; `src/marcxml_export_pipeline/sierra/marcxml.py` is the secondary.
- Byte-compare M2 RDF output vs the pre-plan baseline (same harness as Phase A.5).

**C.5 verification**: byte-equal M2 output on the dev sample; `make lint && make test` green.

**C.5 rollback**: revert C.5; restore Phase B's `lxml>=6` floor to `>=5.2,<6` or revert B.

### C.6 — python-ulid 2 → 3

- ULIDs are minted in the provenance writer (`src/bffi_pipeline/provenance/writer.py` and downstream). `python-ulid` 3.x tightened the UUID-7 helper surface; verify the call sites with a single grep:
  ```bash
  git grep -nE "from ulid\b|import ulid\b|ulid\.(new|from_|to_)" src/
  ```
- Adjust any removed-API call sites. Provenance writer's mint path is deterministic; a unit test on a fixed timestamp + Activity input verifies the new ULID library produces the same shape.

**C.6 verification**: provenance writer unit tests green; `make lint && make test` green.

**C.6 rollback**: revert C.6; restore floor or revert B.

### Phase C acceptance

- [ ] C.1 — mypy 2.x absorbed, `mypy --strict src` zero errors.
- [ ] C.2 — pytest 9.x + pytest-asyncio 1.x absorbed, `asyncio_mode = "strict"` set explicitly.
- [ ] C.3 — langchain-openai 1.x absorbed, M6 dev-sample verdicts match baseline.
- [ ] C.4 — sentence-transformers 5.x absorbed, M5 20 k bench within tolerance.
- [ ] C.5 — lxml 6.x absorbed, M2 dev-sample output byte-equal.
- [ ] C.6 — python-ulid 3.x absorbed, provenance writer tests green.
- [ ] All six sub-phases shipped as separate commits (bisect-friendly).

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| A native dep is sdist-only on cp314 for one of our target platforms | Low-medium | A1 is the explicit verification step; the live options at that point (stay on 3.13, build from source, defer P-40) are spelled out before any code change lands. |
| mypy 2.x produces a large new finding surface in `src/` | Medium | The existing `tool.mypy.overrides` block already silences the six third-party modules that historically bled into `--strict`. Real `src/` findings get fixed at the call site, not waived. If the surface is large enough to blow Phase C.1's time budget, the sub-phase can ship in two commits (one for typing fixes, one to bump the floor) — still bisect-friendly. |
| langchain-openai 1.x changes structured-output semantics | Medium-high | C.3's audit checklist explicitly re-runs M6 against the curated dev-sample baseline; structured-output drift surfaces as a verdict-distribution delta. If the rewrite is non-trivial, C.3 forks into C.3a (mechanical API rewrite) + C.3b (structured-output semantics audit), and C.3b may need to compose with proposal P-06. |
| `sentence-transformers` 5.x changes MPS device defaults silently → M5 falls back to CPU → wall time triples | Medium | C.4's 20 k-bench captures wall time + peak MPS memory; a CPU-fallback regression is unmissable in the wall-time delta. |
| `pytest-asyncio` 1.x's mode default change silently no-ops the one async test | Medium | C.2 sets `asyncio_mode = "strict"` explicitly in `pyproject.toml` so the test fails loudly (missing marker) rather than silently passing as sync. |
| Phase A's `uv.lock` regen drifts dep versions beyond Phase B's intent | Low | A3's verification explicitly diffs `uv.lock` and asserts no package-version moves. A move is a signal to investigate, not a green light. |
| `requires-python = ">=3.14"` excludes a contributor on 3.13 | Low | `uv` (the documented toolchain — see `Makefile`, `.github/workflows/ci.yml`) auto-installs 3.14 on `uv sync`; a non-uv contributor would hit a hard error and need `uv python install 3.14`. Acceptable per existing tooling contract. |

## Open issues to close before / during execution

- **Phase C ordering vs proposal P-06 (structured-output backend)** — C.3's langchain-openai 1.x audit may surface evidence relevant to P-06's open questions. If structured-output semantics change materially between 0.x and 1.x, decide at C.3 kickoff whether to (a) ship C.3 as a mechanical wire-level rewrite and defer the structured-output decision to P-06, or (b) fold the structured-output choice into C.3. Recommendation: (a), to keep the breakage-class commits clean.
- **Free-threaded Python (`python3.14t`)** — flagged out of scope for P-40 (see Goal § "Out of scope"). Drafting P-41 to add `python3.14t` to the build matrix is a follow-on; the gating sequence is P-40 → P-41 → any stage proposal that wants in-process threads. Not a P-40 blocker.
- **Should P-40 carry an upper Python bound (`<3.15`)?** No. Floor-only is the existing project convention across all 27 dep entries. Add an upper bound only if a future Python release breaks a specific call site, and even then prefer fixing the call site over capping the floor.
- **Sequencing vs in-flight work** — at plan-base, `docs/plans/in-progress/` contains only `p-35-m3-cascade-follow-ups.md`, which does not touch `pyproject.toml` or `uv.lock`. Safe to interleave. If a new in-progress plan starts touching the dep graph before P-40 ships, re-verify with the Plan-base drift check before kickoff.

## Cross-references

- [`pyproject.toml`](../../../pyproject.toml) — every surface touched by Phases A + B.
- [`uv.lock`](../../../uv.lock) — the source of truth Phase B aligns the floors to.
- [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml) — verified unchanged in Phase A.
- [`docs/tech-stack.md`](../../tech-stack.md) — consolidated toolchain reference; Phase A's commit updates the Python line.
- [`docs/local-inference.md`](../../local-inference.md) — mlx-lm sidecar reference; consulted in A1 / Prerequisites to confirm pipeline-side Python version is independent of mlx-lm's.
- `CLAUDE.md` § "Conventions" — the `mypy --strict` and `ruff` rules; Phase A bumps the `target-version` / `python_version` settings these rules read from.
- [`src/bffi_pipeline/stages/m6/clients.py`](../../../src/bffi_pipeline/stages/m6/clients.py), [`runner.py`](../../../src/bffi_pipeline/stages/m6/runner.py), [`batch.py`](../../../src/bffi_pipeline/stages/m6/batch.py), [`src/bffi_pipeline/stages/m9/picker_chain.py`](../../../src/bffi_pipeline/stages/m9/picker_chain.py), [`requests.py`](../../../src/bffi_pipeline/stages/m9/requests.py) — Phase C.3 `langchain-openai` 1.x audit surface.
- [`src/bffi_pipeline/stages/m5/embed.py`](../../../src/bffi_pipeline/stages/m5/embed.py), [`build.py`](../../../src/bffi_pipeline/stages/m5/build.py) — Phase C.4 `sentence-transformers` 5.x audit surface.
- [`src/bffi_pipeline/validation/marcxml.py`](../../../src/bffi_pipeline/validation/marcxml.py), [`src/marcxml_export_pipeline/sierra/marcxml.py`](../../../src/marcxml_export_pipeline/sierra/marcxml.py) — Phase C.5 `lxml` 6.x audit surface.
- [`src/bffi_pipeline/provenance/writer.py`](../../../src/bffi_pipeline/provenance/writer.py) — Phase C.6 `python-ulid` 3.x audit surface.
- Memory note `m5_process_isolation.md` — M5 already subprocess-isolated, so Phase C.4 MPS-memory regressions are catchable on a 20 k bench without contaminating M6.
- Memory note `curated_dev_sample.md` — the 13-record sample used in Phase A.5 byte-equality, Phase C.3 verdict baseline, and Phase C.5 M2-output baseline.
- Proposal `p-06-structured-output-backend.md` — gated separately, may receive evidence from Phase C.3.

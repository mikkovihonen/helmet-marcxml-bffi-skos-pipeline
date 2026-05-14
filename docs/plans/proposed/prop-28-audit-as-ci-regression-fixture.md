# P-28 — Audit script as CI regression fixture

**Status**: proposed.
**Scope**: 2-3 days (fixture pinning + Make target + CI wiring).
**Proposal-base commit**: `6b6be25`.
**Source data**: `scripts/audit-merge-clusters.py`, `scratchpad/overnight-sample-2026-05-13/`, `scratchpad/merge-cluster-verdicts/`.

## Motivation

We've built `scripts/audit-merge-clusters.py` as a deterministic
classifier with ~10 heuristics covering the dominant M5 false-
positive classes (different-works-same-author, series-volumes-
collapsed, subtitle-divergence, anthology-vs-specific, in-title-year
mismatch, etc.). On the 2026-05-13 overnight bench, the audit
classifies 96 % of merge clusters into named buckets and matches
human inspection on every spot-checked case.

This script is *unrecognised regression infrastructure*. Today:
- It lives in `scripts/`, not in `tests/`.
- It runs by hand against `scratchpad/`-anchored inputs.
- Its verdict distribution is not asserted anywhere.
- A code change to M5's blocking key, embedder input string, or
  cascade decision can silently change the merge-cluster
  distribution — the audit catches it only if someone re-runs it.

Every veto proposal on the board (prop-20, prop-23 through prop-26)
states a regression criterion of the form *"on the 2026-05-13
overnight sample, the N audit-flagged FP rows must escalate"*. With
the audit-as-CI-fixture, those criteria become CI-enforceable.
Without it, they're aspirational comments in a markdown file.

A second, larger problem: when a veto ships, its first effect is to
shift the audit's verdict distribution. Today there's no way to
tell whether the shift matches the proposal's prediction or breaks
something orthogonal. A pinned baseline turns each veto PR into a
diff: "the audit moved 40 rows from `different_works_same_author` to
`escalated` — matches prop-22's prediction".

## Approach

Three phases. Phases A + B are the load-bearing CI integration;
Phase C is the periodic re-bench cadence.

### Phase A — Pin the audit fixture

Create `tests/fixtures/audit-bench-2026-05-13/` containing:
- `marcxml/` — the MARCXML source records that produced the 183
  merge clusters (a subset of the 19 570-record overnight bench;
  estimated ~400 records × ~3 KB each ≈ 1.2 MB on disk).
- `canonical-map.jsonl` — frozen M8 output for these records.
- `merge-clusters.csv` — frozen SPARQL output (the input to the
  audit script).
- `expected-verdicts.jsonl` — frozen audit output. The regression
  baseline.

Selection: extract the cluster set from
`scratchpad/overnight-sample-2026-05-13/merge-clusters.csv`, walk to
the source MARCXML, copy into the fixture. Document the extraction
script as `scripts/build-audit-fixture.py` so future re-bench
operations are reproducible.

The fixture is **immutable per veto proposal**. When prop-22 ships,
it adds `tests/fixtures/audit-bench-2026-05-13-post-prop-22/` with
the new `expected-verdicts.jsonl` (40 rows moved from
`different_works_same_author` to `escalated`). The pre-prop-22
fixture stays in place — it's the regression oracle for the prop-22
PR itself.

### Phase B — `make audit-test` target + CI wiring

New Make target:

```makefile
audit-test:
	uv run python scripts/audit-merge-clusters.py \
	    --csv tests/fixtures/audit-bench-2026-05-13/merge-clusters.csv \
	    --marcxml-dir tests/fixtures/audit-bench-2026-05-13/marcxml \
	    --out-dir /tmp/audit-test-output
	uv run python scripts/compare-audit-verdicts.py \
	    --expected tests/fixtures/audit-bench-2026-05-13/expected-verdicts.jsonl \
	    --actual /tmp/audit-test-output/verdicts.jsonl
```

`compare-audit-verdicts.py` exits non-zero on any deviation from the
baseline. Verdict-distribution-level comparison first (counts per
class); per-row diff secondary (which rows moved categories). The
CI run logs both.

CI wiring: `.github/workflows/audit.yml` (or extend the existing
test workflow) runs `make audit-test` on every PR that touches:
- `scripts/audit-merge-clusters.py`
- `src/bffi_pipeline/stages/embeddings.py`
- `src/bffi_pipeline/stages/merge.py`
- `src/bffi_pipeline/text/`
- `sparql/`

PRs that don't touch any of these skip the audit (saves CI time).

### Phase C — Periodic re-bench

Once the veto stack ships, the audit fixture grows stale: new
production behaviour produces new merge clusters that the fixture
doesn't represent. Re-bench cadence:
- **Per major release** of the pipeline (M5 / M6 / M8 logic change).
- **After every K=5 vetoes shipped** (so the audit fixture stays in
  sync with the cascade).
- **Operator-triggered** via `make rebench-audit`.

The re-bench is *not in CI* — it runs against the full
20 k MARCXML corpus and takes wall-time hours (current bench: ~5 h).
It runs locally on the M5 Max. Output:
`scratchpad/audit-rebench-<date>/`. When the output is committed,
the fixture's `expected-verdicts.jsonl` is updated as part of the
same PR.

### Why the audit fits CI (and M5/M6 don't)

CLAUDE.md says LLM eval doesn't run in CI. The audit is **not** an
LLM eval — it's a deterministic Python classifier reading frozen
JSON / Turtle / MARCXML files. CI requirements:
- ~10-second wall on the fixture (183 clusters × ms-level
  classification).
- ~2 MB fixture data on disk.
- Zero LLM / Fuseki / network dependency.
- Pure Python; same deps as the audit script.

Easy fit.

## Phases (operational order)

**A.1 Fixture-build script.** `scripts/build-audit-fixture.py`
reads a `merge-clusters.csv` + `marcxml-dir` + `canonical-map.jsonl`
and emits `tests/fixtures/audit-bench-<date>/`. ~80 lines.

**A.2 Run fixture-build against 2026-05-13.** Commit
`tests/fixtures/audit-bench-2026-05-13/`.

**B.1 Verdict-comparison script.**
`scripts/compare-audit-verdicts.py` reads expected + actual,
exits 0 on match, 1 on distribution drift, 2 on row-level drift.
~50 lines.

**B.2 `make audit-test` target.**

**B.3 CI workflow extension** for path-scoped triggering.

**C.1 `make rebench-audit` target** (manual cadence; no CI).

**C.2 Operator runbook** at `docs/operator-runbook.md` (if absent,
add a section): when to re-bench, how to update the fixture.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (graduated 2026-05-14; see ../in-progress/), and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; pinning a bench as a CI fixture before verifying its surfaces are non-misleading would freeze the misleading numbers into the regression baseline. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- 2026-05-13 overnight bench data preserved at
  `scratchpad/overnight-sample-2026-05-13/`. Don't delete until the
  fixture is committed.
- Per `CLAUDE.md`'s "tests against fixtures, not network" rule, no
  Fuseki dependency in the fixture build — the fixture freezes
  Fuseki's output, doesn't query Fuseki at test time.

## Risks

- **R1 — Fixture rot.** As the cascade evolves, the fixture stops
  reflecting production behaviour. Phase C's periodic re-bench is
  the mitigation; the K=5-veto cadence is the operational rule of
  thumb.
- **R2 — Fixture size growth.** If we pin one fixture per major
  veto, 5 vetoes = 5 × 1.2 MB = 6 MB in the repo. Acceptable.
  Alternative: delete superseded fixtures on veto graduation.
  Recommend: keep at most 2 in tree (pre-change baseline + post-
  change baseline); archive older ones.
- **R3 — False sense of security.** CI passing the audit-test
  doesn't mean the cascade is correct — it means the cascade hasn't
  regressed *on this fixture*. New record patterns the fixture
  doesn't cover (e.g. Hebrew / Korean MARCXML when Helmet expands)
  are still untested. Mitigation: the audit is a regression check,
  not a coverage proof. Periodic re-bench against the full corpus
  is what closes the coverage gap.
- **R4 — Per-row diff noise.** Two audit runs may produce the same
  distribution but different cluster identities (e.g. canonical Work
  URI changes because M8's union-find tie-breaking shifts). The
  comparison should be cluster-identity-stable: compare by `bib_ids`
  set rather than by canonical Work URI.
- **R5 — Audit-script bugs masked by fixture.** If the audit script
  has a bug that produces a stable-but-wrong verdict, the fixture
  blesses the bug. Mitigation: per-veto-PR re-spot-check by hand,
  same way prop-26's audit refinement was checked.

## Open questions

- Single fixture or one-per-veto? Recommend one-per-veto, with a
  rolling 2-fixture window in tree.
- Should the comparison script use JSON-schema-style verdict
  semantics, or just byte-equal the JSONL? Distribution-level + key
  field comparison is enough; full byte-equal makes the fixture too
  brittle to ordering changes.
- Does the fixture need to include the BFFI Turtle output too?
  Probably not — the audit reads MARCXML and `canonical-map.jsonl`,
  not BFFI Turtle.
- Should `make audit-test` run as part of `make test` or stay
  separate? Stay separate — `make test` is fast (< 30 s);
  `make audit-test` is also fast (~10 s) but conceptually a
  different scope. CI runs both; local devs typically run only
  `make test`.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `tests/fixtures/audit-bench-2026-05-13/` committed with
      MARCXML + merge-clusters.csv + canonical-map.jsonl +
      expected-verdicts.jsonl.
- [ ] `scripts/build-audit-fixture.py` and
      `scripts/compare-audit-verdicts.py` committed.
- [ ] `make audit-test` runs in < 15 s on the fixture; fails on
      any class-count drift.
- [ ] CI workflow runs `make audit-test` on PRs touching the path-
      list in Phase B.3.
- [ ] First veto PR (e.g. prop-23 numeric markers) explicitly
      updates `expected-verdicts.jsonl` in the same commit, with the
      diff matching the proposal's prediction.

## What this proposal does NOT do

- Doesn't replace `make eval`'s LLM-gold-set evaluation. That's a
  fixed quality bar for prompt / model / judge changes; this is a
  cascade behaviour fixture.
- Doesn't audit M6 verdicts (prop-27).
- Doesn't audit recall (prop-29).
- Doesn't try to be a comprehensive integration test for the
  pipeline. Stage-level integration testing is the spec's
  responsibility; this is *one specific regression surface* (the
  audit's verdict distribution).

## Composition with sibling proposals

- **Enables every veto proposal.** Without prop-28, the regression
  criteria in prop-20 / 22 / 23 / 24 / 25 / 26 are aspirational.
  With prop-28, they're enforceable.
- **Composes with prop-27.** prop-27 produces an M6 agreement matrix
  as a one-shot writeup. If the M6 sub-audit also turns out to be
  worth running periodically, a Phase D could extend prop-28's
  fixture pattern to M6 verdicts (separately, because LLM verdicts
  don't run in CI — the cadence would be local + manual).

# P-06 — Gold-set growth to 50-100 cataloguer-vetted cases

**Status**: draft.
**Source**: `docs/BUILD_PLAN.md` M12 unfinished item at L452 (gold-
set growth toward 50-100 cases with per-category holdout
stratification). Not graduated from a proposal — this is committed
M12 work that needs its own plan because the work is mostly external
(cataloguer labour, not codeable) but has hard prerequisites for
P-01, P-04, and P-05.
**Plan-base commit**: `fe0b8dd`. To gauge drift, run
`git diff fe0b8dd..HEAD -- gold/ src/bffi_pipeline/eval/`.
**Phase commits**:

- Phase A (candidate generation from cataloguer overrides): `<unfilled>`
- Phase B (cataloguer review + merge): `<unfilled>`
- Phase C (holdout-stratification audit): `<unfilled>`

**Owner**: TBD on the technical side; Helmet cataloguers on the
review side.
**Estimated wall-time**: 1-2 days of engineering work (mostly
review-pipeline polish + audit script). The cataloguer effort is
roughly 100-200 case-reviews at ~5-10 minutes each → 8-30
person-hours; that's the wall-time bottleneck.

## Goal

Grow `gold/gold.jsonl` from the current 17 bootstrap cases to 50-100
cataloguer-vetted cases that:

- Stratify across the `GoldCategory` literal values (translation,
  transliteration, adaptation, abridgement, common-title-collision,
  compilation-vs-constituent, edition-revision,
  music-recording-vs-notated, same-author-different-titles,
  cross-genre-different-work, subject-as-name-discrimination —
  per `src/bffi_pipeline/eval/gold_set.py`).
- Hold out 30 % hand-marked (`"holdout": true`), with **every
  category carrying ≥ 2 holdout cases** per spec § 9. The current
  bootstrap doesn't meet the per-category min-2 holdout requirement.

## Definition of done

- `gold/gold.jsonl` has 50-100 cases.
- Every `GoldCategory` value has ≥ 2 holdout cases.
- Total holdout share is 25-35 %.
- `assert_holdout_stratification()` in `src/bffi_pipeline/eval/gold_set.py`
  passes with `min_per_category=2`.
- A CI lint step asserts these invariants on every push to `gold/`.

## Current state

- Bootstrap of 17 cases at `gold/gold.jsonl` covering 7-9
  categories with ~31 % holdout overall but failing the
  per-category min-2 requirement.
- `src/bffi_pipeline/eval/grow.py` and CLI `bffi-pipeline grow-gold`
  are committed: read cataloguer-overridden M6 decisions from
  Fuseki, output `gold/grow-candidates.jsonl` with one row per
  override. New cases default to `"holdout": false`; the cataloguer
  flips the flag and fills in `category`.
- `gold/README.md` documents the convention.

The gating issue: cataloguer overrides only accumulate as the
pipeline runs over real corpora and cataloguers correct M6
decisions in Skosmos. The preview-373 run and the in-flight v2
full-corpus run will produce these overrides naturally once they
load.

## Strategy

Two parallel sources of candidate cases — accept whichever produces
material faster:

1. **Cataloguer-override candidates** via `grow-gold`. Produced as
   a byproduct of cataloguers reviewing Skosmos and correcting M6
   verdicts. Highest-value cases (cataloguer caught an error the
   pipeline made), but throughput depends on cataloguer review
   cadence.
2. **Bib-type-diversity targeting**. Cataloguers volunteer specific
   bib pairs they think are illustrative — same shape as the
   gs-0016 / gs-0017 cases the cataloguers shared (Kunnas Koirien
   Kalevala, Sarkia Runot). These don't require Skosmos overrides;
   the cataloguer just lists bib IDs + expected verdict + category.

---

## Phase A — Candidate generation polish

Estimated wall-time: half a day.

### A1. Tighten `grow-gold` output shape

Current `gold/grow-candidates.jsonl` schema (per the existing
`grow.py`):

```json
{"helmet_bib_id_a": "...", "helmet_bib_id_b": "...",
 "category": null, "expected": "same_work" | "different_work",
 "llm_decision": "...", "llm_rationale": "...",
 "override_decision": "...", "override_actor": "...",
 "override_timestamp": "..."}
```

Confirm:

- `expected` is the inverse of `llm_decision` (cataloguer
  overrode), not equal — the harness counted on the inversion.
- `category` defaults to `null` (cataloguer fills in).
- `holdout` is **not** in the output — defaults to `false` when
  the cataloguer merges into `gold.jsonl`.

If anything drifted, fix and add a regression test.

### A2. Add a `bib-pairs-to-candidates` CLI

For source 2 (cataloguer-volunteered bib pairs), add a small CLI
that reads a TSV of `bib_id_a, bib_id_b, expected, category` rows
and produces a JSONL output the cataloguer can review + merge.
Each candidate row pulls the bib's title / creator / language /
content_type from the canonical graph in Fuseki to populate
`record_a` and `record_b` automatically.

CLI: `bffi-pipeline gold-candidates-from-pairs --input <tsv>
--output gold/grow-candidates.jsonl`.

This makes the cataloguer's "here's a bib pair I want in the gold
set" workflow a single TSV row, not hand-edited JSON.

### A3. Phase A acceptance

- [ ] `bffi-pipeline grow-gold` schema regression-tested.
- [ ] `bffi-pipeline gold-candidates-from-pairs` exists with unit
      tests against a mock Fuseki.
- [ ] `gold/README.md` documents both ingestion paths.

---

## Phase B — Cataloguer review + merge

Estimated wall-time: 8-30 person-hours of cataloguer effort, spread
over weeks. Engineering effort: ~negligible (just answering
cataloguer questions).

### B1. Run grow-gold against the v2 corpus

After v2 finishes loading into Skosmos and cataloguers have a
chance to review and override some M6 decisions:

```bash
uv run bffi-pipeline grow-gold \
    --fuseki-url http://localhost:3030/bffi \
    --output-path gold/grow-candidates.jsonl
```

### B2. Cataloguer hand-merge

Cataloguer opens `gold/grow-candidates.jsonl`, picks cases worth
adding, fills in `category`, sets `holdout` per the
stratification audit (see Phase C). Merges to `gold/gold.jsonl`
via a PR. The PR template's eval block (existing) will trigger a
`make eval` run.

### B3. Phase B acceptance

- [ ] At least one round of `grow-gold` candidates has been
      offered to cataloguers.
- [ ] At least one cataloguer PR has merged growth cases into
      `gold/gold.jsonl`.
- [ ] Per-cataloguer-volunteer pair: at least one TSV ingestion
      round has gone through and merged.

---

## Phase C — Holdout-stratification audit

Estimated wall-time: half a day.

### C1. Audit script

Add `src/bffi_pipeline/eval/gold_audit.py` (or extend
`gold_set.py`) with a CLI:

```bash
uv run bffi-pipeline gold-audit
```

That prints:

- Total case count.
- Per-category breakdown (count + holdout count).
- Total holdout share.
- A bulleted list of categories that fail `min_per_category=2`
  holdout, with suggested promotions from training to holdout.

### C2. CI gate

Add a CI step to `.github/workflows/ci.yml` that runs `gold-audit`
in `--strict` mode on every push touching `gold/`. Fails if:

- Total holdout share is outside `[20 %, 40 %]`.
- Any category present has fewer than 2 holdout cases.

This stops the gold set from regressing on stratification.

### C3. Phase C acceptance

- [ ] `gold-audit` CLI exists with tests.
- [ ] CI step gates `gold/gold.jsonl` PRs on stratification.
- [ ] First green run on a PR that adds gold-set cases passing
      the strict check.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cataloguer review throughput is too low | Medium-high | Reduce review burden per case: pre-populate `record_a` / `record_b` via Fuseki lookup; cataloguer only fills in `category` + `holdout`. Phase A is the engineering knob. |
| Categories never hit min-2 holdout because corpus doesn't surface them | Low-medium | Source 2 (cataloguer-volunteered pairs) covers categories the corpus doesn't naturally produce. |
| Cataloguer-volunteered pairs all cluster in one category | Medium | The TSV ingestion intake should display the per-category histogram so the cataloguer can self-direct toward underrepresented categories. |
| Stratification gate too strict, blocks otherwise-good PRs | Low | Audit script reports the failure mode clearly; cataloguer flips a `holdout` on an existing training case to rebalance. CI failure message is actionable. |

## Open issues to close before / during execution

- Spec § 9 mandates "every category needs at least 2-3 hold-out
  cases". Pick 2 or 3 as the strict gate? Recommend 2 for the
  growth phase, raise to 3 once the gold set crosses 100 cases.
- When the gold set crosses ~500 cases (probably 12-24 months
  away), drop the holdout share to 20 % per the spec's own
  guidance. Out of scope here.
- Should categories that don't appear in the corpus at all be
  excluded from the stratification gate, or kept as a forcing
  function for source-2 volunteer pairs? Recommend the latter —
  the empty-category signal is informative.

## Cross-references

- `docs/BUILD_PLAN.md` M12 — origin checklist item.
- `docs/marcxml-to-bffi-skosmos-pipeline.md` § 9 — gold-set
  stratification requirement.
- `gold/README.md` — cataloguer-facing instructions; update with
  the dual-source workflow once Phase A ships.
- `src/bffi_pipeline/eval/grow.py` — existing growth CLI.
- P-01 — distillation pre-screener whose training premise depends
  on this gold set growing.
- P-05 phase F3 — gated on a separate `gold/contrib.jsonl` reaching
  30-50 cases; conceptually similar work but a different artifact.

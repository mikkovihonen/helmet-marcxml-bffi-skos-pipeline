# P-30 — Critical audit of observability + audit-trail practices

**Status**: proposed.
**Scope**: 2-3 days end-to-end (catalogue surfaces, define ground truth, run drift checks, write up).
**Proposal-base commit**: `6b6be25`.
**Trigger incident**: 2026-05-13 overnight bench `used_cascade` misread (see prop-27 Motivation).

## Motivation

The 2026-05-13 overnight bench provided a near-miss: prop-27 was
about to ship around the premise "M6 fired zero times" because
`judge-decisions.jsonl`'s `used_cascade: false` flag was read at face
value. The actual cache (`judge-cache.sqlite`) showed 988 LLM
invocations. Two observability surfaces published *directly
contradictory* readings for the same run, and the misleading one was
the more discoverable.

This is not the first such incident on the project:

- **prop-17** documents a five-hour observability blackout during
  the same bench's launch (exporter resolved its sidecar path at
  startup; pipeline wrote events to a different `BFFI_DATA_DIR`).
- **prop-18** documents the M8 ``pending`` mis-display during the
  8-minute corpus load — operator sees an idle tile while the
  process is fully busy.
- **prop-19** documents M8 corpus-load throughput, which the
  dashboard wasn't surfacing as a wall-time concern because no
  metric tracked it.

A pattern emerges: **our observability surfaces routinely publish
plausible-looking but wrong readings**, and the project has been
operating partly on faith that they're correct. Every audit /
proposal / decision that consumes a number from the dashboard, a
sidecar field, a stage-events row, a Grafana panel, or the
provenance graph is conditionally compromised.

The veto stack (prop-20 / 23 / 24 / 25 / 26) and the auditing stack
(prop-27 / 28 / 29) all consume bench numbers that haven't been
sanity-checked against ground truth. Before any of those ship, we
need to know which numbers we can trust and which we can't.

## Approach

A two-track critical review with hard deliverable: a corrected,
ground-truth-anchored mapping of every observability surface the
pipeline produces.

### Track A — Catalogue the observability surfaces

Enumerate every place the pipeline emits or persists machine-
readable signal:

- **Live**: Prometheus metrics, Grafana panel queries, the local
  exporter sidecar, the stage-events tail.
- **Run artefacts**: `stage-events.jsonl`, `judge-decisions.jsonl`,
  `judge-cache.sqlite`, `reconcile-cache.sqlite`,
  `canonical-conflicts.jsonl`, `canonical-map.jsonl`,
  `embed-candidates.jsonl`, `manifest.json`, `pipeline.log`.
- **Persistent state**: the PROV-O graph in Fuseki
  (`bffi-prov`-namespaced triples per CLAUDE.md), the
  `bffi:adminMetadata` blocks on canonical Works/Expressions.
- **Counters surfaced through the CLI**: `bffi-pipeline status`,
  `bffi-pipeline doctor`, end-of-stage `render()` outputs.

For each surface: name, file/endpoint, schema, intended meaning,
emit site in code.

### Track B — Spec ground truth per surface

For each surface, write down what it *should* report — independently
of what the field is named. This is the load-bearing step. The
`used_cascade` example:

| Field | Surface | Intended meaning | What it actually reports |
|---|---|---|---|
| `used_cascade` | `judge-decisions.jsonl` | (misread as) "M6 LLM judge fired" | "did the 8B → 72B fallback engage" (always false on M5 Max — 72B doesn't fit) |
| Authoritative signal | `judge-cache.sqlite` row count + `cascade.stage = llm-judge-primary` count | "M6 LLM judge fired N times" | same — agrees with itself |

Drift between intended and actual is what the audit captures.
Sources of drift, in observed frequency:

1. **Flag-naming gotchas** (`used_cascade`-class). Flag names that
   sound like one thing but report another.
2. **Path-resolution gotchas** (prop-17 class). Sidecars / event
   files written to a path the consumer doesn't read.
3. **Lifecycle gotchas** (prop-18 class). Events emitted at the
   wrong point in a stage's lifecycle.
4. **Coverage gotchas** (prop-19 class). Operations that have no
   metric at all, surfacing as silent slow paths.
5. **PROV-O / `bffi:adminMetadata` divergence**. The provenance
   graph claims one verdict; the JSONL emits another. Untested.
6. **Idempotency-skip silent counters**. Stages that skip
   (idempotency check) still emit `start` / `end` events with
   counters from the previous run — operator can't tell a skip
   apart from a real fast run.

### Track C — Drift checks against ground truth

For each surface, run an automated cross-check on the 2026-05-13
bench artefacts:

- **Counter integrity**. `stage-events.jsonl` end-counter for stage
  X should equal the row count of stage X's persisted output. M6
  example: `stage=judge end counters.judged` should equal
  `judge-cache.sqlite` row count.
- **Decision integrity**. For pairs in `judge-decisions.jsonl`, the
  `decision` field should match the M6 cache verdict (when one
  exists) AND the `bffi:adminMetadata` AND the PROV-O
  `bffi-prov:decision` triple.
- **Timestamp integrity**. Stage start/end events should bracket all
  the writes the stage actually made (no writes outside the bracket).
- **Idempotency separation**. A skip event should be distinguishable
  from a real run event (separate `skipped: true` field or a
  distinct event name).

Each check produces a pass/fail row. The deliverable's headline
metric is "number of surfaces × number of drift checks passed /
total".

### Track D — Document the corrected readings + name the gotchas

Output: `docs/observability-truth-table.md`. Per surface:
- What the field is named.
- What it actually measures.
- Which surface to read instead if the name is misleading.
- Whether the misleading name should be renamed (prop-31 candidate)
  or left in place with a documentation patch.

This document becomes the authoritative consumer-facing reference
for every audit / dashboard / proposal that reads pipeline state.

## Phases

**A.1 Catalogue.** Enumerate every observability surface; emit a
table at `docs/observability-surfaces.md`. ~1 day; the spec
already implicitly names most of these.

**A.2 Ground-truth spec.** For each surface, write its intended
meaning. ~half day.

**B.1 Drift-check harness.** A new script
`scripts/audit-observability.py` runs the per-surface cross-checks
against a bench artefact set. ~1 day to write, runs in seconds.

**B.2 Run on the 2026-05-13 bench.** Outputs
`scratchpad/observability-audit-2026-05-13/{drift-report.jsonl,
summary.md}`.

**C.1 Truth-table document.** `docs/observability-truth-table.md`
written from the drift report. Per surface: name, actual meaning,
recommended-reading alternative.

**C.2 Operator handoff.** Mark the document in the project's top-
level README / CLAUDE.md / `docs/operator-runbook.md` as the
required reading for anyone consuming a pipeline number.

**D.1 Issues filed (or proposals queued)** for each drift the
audit surfaces. Fixes themselves are out of scope for prop-30; the
audit produces a list of remediation work — small things become
patches, big things become prop-31+.

## Prerequisites

- **prop-17 implemented.** The exporter's sidecar resolution must
  be in its final shape; auditing a surface that's about to be
  reshaped is wasted work.
- **prop-18 implemented.** M8's lifecycle events must be at the
  right boundaries before any timestamp-integrity check is
  meaningful.
- **prop-19 implemented.** M8 throughput is currently silent; the
  audit should ground-truth the post-prop-19 counters, not the
  pre-prop-19 missing ones.
- The 2026-05-13 bench artefacts (preserved at
  `scratchpad/overnight-sample-2026-05-13/`) AND a fresh post-
  prop-17/18/19 bench. The audit compares pre/post on every surface.

## Risks

- **R1 — Audit-surface coverage gap.** The catalogue inevitably
  misses something (a `pipeline.log` line that has its own implicit
  schema, a Grafana panel reading a metric we don't list). Mitigation:
  the catalogue is the *first cut*; iterate with operator review
  before signing off.
- **R2 — Some drift is intentional.** Idempotency-skip events
  reusing the previous-run counters is arguably correct (the
  numbers are still meaningful). The audit must distinguish "drift
  is a bug" from "drift is design".
- **R3 — Truth-table maintenance burden.** A new field or surface
  added later isn't automatically in the truth table. Mitigation:
  add a lint check that any new event-schema field be referenced
  in `docs/observability-truth-table.md` before the PR merges.
- **R4 — Performance overhead of the drift-check harness.** Has to
  read the full M6 cache + every JSONL on the bench. Estimated ~10 s
  on 20 k bench; ~5 min on 800 k. Run on bench artefacts, not in
  live CI.

## Open questions

- Should the truth-table document live in `docs/` or be embedded as
  field-level comments next to each emit site? Document for
  consumer-facing clarity; the emit site is for code-review-time.
- Does the PROV-O graph need its own audit pass, or does the
  decision-integrity check cover it? Probably needs its own:
  PROV-O has structural constraints (every `prov:Activity` has at
  least one `prov:used` + at least one `prov:wasGeneratedBy`,
  etc.) the JSONL audit won't check.
- Should the drift-check harness become part of prop-28's CI
  fixture pattern? Yes — once the harness is stable, freeze a
  drift-report.jsonl and assert it stays in shape across PRs.
  Defer the wiring until prop-28's pattern lands.
- Does the audit reveal anything that motivates *new* observability
  rather than fixing existing? Possible — e.g. a missing "M6
  cache-hit rate" metric. List as findings; specific additions
  become their own proposals.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `docs/observability-surfaces.md` catalogues every machine-
      readable signal the pipeline emits or persists.
- [ ] `scripts/audit-observability.py` runs cross-checks on a
      bench artefact set.
- [ ] `scratchpad/observability-audit-2026-05-13/` committed.
- [ ] `docs/observability-truth-table.md` documents every misleading
      field's actual meaning + recommended-reading alternative.
- [ ] Fresh post-prop-17/18/19 bench run, audit re-run; any drift
      fixed by remediation patches or queued as prop-31+.
- [ ] Operator runbook references the truth table as required
      reading for anyone consuming pipeline numbers.
- [ ] Sign-off recorded: "observability surfaces no longer
      misleading; downstream audit work (prop-20 through prop-29)
      may proceed."

## What this proposal does NOT do

- Doesn't fix every drift it finds. Fixes are downstream
  remediation work, queued as separate proposals or patches.
- Doesn't redesign the observability stack. The local-only
  Prometheus + Grafana + sidecar architecture stays.
- Doesn't add new metrics for their own sake. New metrics happen
  if and only if the audit identifies a coverage gap that blocks
  a downstream proposal.
- Doesn't audit non-pipeline observability (Fuseki / Skosmos
  service-level metrics). Those live outside the BFFI pipeline's
  signal surface.

## Composition with sibling proposals

- **Gates the entire FP / audit stack.** prop-20 through prop-29
  all consume bench numbers. Until prop-30 establishes which
  numbers are trustworthy, downstream audit work risks repeating
  the prop-27 `used_cascade` near-miss.
- **Sequenced after prop-17, prop-18, prop-19.** Auditing
  observability before those land would audit surfaces about to be
  reshaped.
- **Feeds prop-28.** Once the drift-check harness exists, its
  fixture can sit alongside prop-28's audit fixture as a second CI
  regression surface.
- **May spawn prop-31+.** Each non-trivial drift becomes its own
  remediation proposal.

# P-27 — M6 LLM judge verdict audit (against the heuristic baseline)

**Status**: proposed.
**Scope**: 1-2 days (audit script extension on existing data + writeup).
**Proposal-base commit**: `6b6be25`.
**Source data**: `scratchpad/overnight-sample-2026-05-13/judge-cache.sqlite` (988 M6 verdicts), `scratchpad/overnight-sample-2026-05-13/judge-decisions.jsonl` (1342 cascade decisions), `scratchpad/merge-cluster-verdicts/verdicts.jsonl` (audit baseline).

## Motivation

The 2026-05-13 20 k overnight bench exercised M6 heavily — 988 LLM
judge invocations against the Qwen3-8B-4bit model between 18:39 and
19:34 UTC. Two observability surfaces produce inconsistent readings:

| Source | Reading | Interpretation |
|---|---|---|
| `judge-decisions.jsonl` `used_cascade: false` (1342 rows) | "no cascade engaged" | (misread as "M6 dormant") |
| `judge-decisions.jsonl` `cascade.stage = llm-judge-primary` | **988 pairs** | M6 fired 988 times |
| `judge-cache.sqlite` row count | **988 rows** | M6 fired 988 times |
| Cache row timestamps | 2026-05-13T18:39 → 19:34 UTC | M6 ran ~55 min during the bench |

The `used_cascade` flag means "did the cascade fall back to the
larger model" — and because the M5 Max can't fit Qwen3-72B (dev
machine constraint), that fallback is structurally always off.
Reading the flag as "did the LLM judge run at all" is wrong but
easy to do; this is the kind of misleading observability surface
P-17 was about.

The actual bench distribution:

| Band | Count | Source |
|---|---:|---|
| auto-merge (sim ≥ 0.90) | 354 | M5 embedding alone |
| **escalate (0.78 < sim < 0.90)** | **988** | **M6 LLM judge** |
| auto-reject (sim ≤ 0.78) | not represented in this file | M5 dropped before judge stage |

M6 cache verdict split: **83 `same_work` + 905 `different_work`**.

This reframes everything the merge-cluster audit (P-26's source
data) measured:

1. **The 51 % FP rate is M5 + M6 *combined*.** Of the 183 merge
   clusters the audit classified, some emerged from M5's 354 auto-
   merges and some from M6's 83 `same_work` verdicts (folded into
   M8's canonical map). M6 is already part of the FP surface, not a
   downstream gate to be tested.

2. **P-21's Aalto hallucination is more representative than n=1.**
   M6 said `same_work` 83 times during this bench. If a non-trivial
   share follow the P-21 pattern (subject-of-art-book → same Work
   hallucination), M6 is leaking same-Work calls into the canonical
   map at a rate worth measuring.

3. **The five-veto stack (P-20/23/24/25/26) doesn't push traffic
   into a dormant gate — it shifts load that's already there.** Each
   veto demotes some auto-merges to escalate; M6 then judges. The
   open question is no longer "will M6 fire?" but "is M6's verdict
   reliable on the patterns the vetoes escalate?"

## Approach

The data already exists. Audit the 988 M6 verdicts against the same
heuristic classifier that produced the 183-cluster merge audit. No
force-firing needed — we have a full run of production verdicts.

### A — Audit the 83 `same_work` M6 verdicts (precision side)

For each cache row with `decision: same_work`, locate the two
records in the bench, run them through the audit script's
classification heuristics, and record the matrix:

| Audit verdict | M6 verdict |
|---|---|
| `legitimate_translation` / `legitimate_reedition` | `same_work` ✓ (good agreement) |
| `different_works_same_author` / `subtitle_divergence` / `subject_misread` / etc. | `same_work` ✗ (M6 FP) |

**Output:** `scratchpad/m6-verdict-audit/precision.jsonl`. The
**M6 FP rate** is what fraction of the 83 disagree with the
heuristic in the FP direction. If this rate is comparable to M5's
auto-merge FP rate (~51 %), M6 isn't materially better than M5 on
the escalate band and we have a deeper problem than the veto stack
addresses. If it's substantially lower (say < 15 %), M6 is roughly
working and the veto stack is well-founded.

### B — Audit the 905 `different_work` M6 verdicts (recall side)

For each cache row with `decision: different_work`, apply the same
classification. Verdict matrix:

| Audit verdict | M6 verdict |
|---|---|
| `different_works_same_author` / `series_volumes_collapsed` / `subject_misread` / etc. | `different_work` ✓ (good agreement) |
| `legitimate_translation` / `legitimate_reedition` | `different_work` ✗ (M6 FN) |

**Output:** `scratchpad/m6-verdict-audit/recall.jsonl`. The
**M6 FN rate** estimates how many legitimate same-Work pairs M6 is
splitting apart. Together with P-29's recall audit, this
quantifies M6's contribution to the false-negative surface.

### C — Examine M6 vs M5 disagreement on the escalate band

For each of the 988 escalate-band pairs, M5's similarity carries an
implicit verdict ("probably same_work" at sim ≈ 0.89, "probably
different" at sim ≈ 0.79). Plot M6 verdict against M5 similarity.
Expected: M6 leans `same_work` above 0.85 and `different_work`
below. Surprising patterns (e.g. M6 says `different_work` at sim
0.89, or `same_work` at sim 0.80) flag pairs worth manual review —
either M5's similarity is misleading or M6's verdict is off.

### Deliverable

`docs/performance/<date>-m6-verdict-audit.md`:
- Precision matrix (sub-audit A) with the M6 FP rate.
- Recall matrix (sub-audit B) with the M6 FN rate.
- M6-vs-M5 disagreement scatter (sub-audit C).
- Recommendation: ship the veto stack as-is / harden M6 first /
  narrow the veto coverage.

## Approach

Run an offline M6 quality audit *before* the veto stack lands. Two
sub-audits, both consuming the existing 20 k bench:

### A — Force-fire M6 on the audit-flagged FP set

Take the 93 audit-flagged FP clusters (40 + 34 + 11 + 3 + 2 + 2 + 1
from P-26's verdict distribution, minus the 64 legitimate-
reedition + 20 legitimate-translation true-positives). For each
cluster, construct the canonical M6 input — work_a + work_b BFFI
fragments, prompt template, embedding similarity — and invoke the
judge synchronously through `src/bffi_pipeline/stages/judge.py`. Log
the verdict against the audit's heuristic verdict.

```python
# Pseudo-flow
audit_rows = load("scratchpad/merge-cluster-verdicts/verdicts.jsonl")
fp_rows = [r for r in audit_rows if r["verdict"] not in
           {"legitimate_reedition", "legitimate_translation", "uncertain"}]
for row in fp_rows:
    a, b = sample_pair_from_cluster(row)  # first two records of the cluster
    m6_verdict = judge.evaluate(a, b, similarity=row["similarity"])
    log({
        "cluster": row["canonical_work_uri"],
        "audit_verdict": row["verdict"],
        "m6_verdict": m6_verdict.decision,
        "m6_confidence": m6_verdict.confidence,
        "m6_rationale": m6_verdict.rationale,
    })
```

**Output:** `scratchpad/m6-baseline-audit/m6-vs-audit-disagreement.jsonl`
— one row per audit-flagged FP cluster. The KEY metric is the
**agreement matrix**:

|   | M6: same_work | M6: different_work | M6: uncertain |
|---|---:|---:|---:|
| Audit: FP class | (M6 missed it) | (M6 catches it ✓) | (escalate to human) |

If the diagonal (`different_work` for audit-FPs) is high — say ≥ 80 % —
the veto stack is safe to ship. If the off-diagonal `M6: same_work`
column is dominant, M6 hallucinates same-Work *systematically* on
the patterns the vetoes escalate (P-21 generalised). That blocks
the veto stack until M6's prompt or model is hardened.

### B — Auto-reject band audit (recall side)

Sample 50-100 of the 905 `different_work` auto-rejects from the bench
and apply the same audit heuristics in reverse: for each rejected
pair, ask "would a cataloguer agree?" The audit script's
`legitimate_reedition` / `legitimate_translation` rules can run on
the rejected pair just as well as on merged clusters. If we find
auto-rejects that look like legitimate same-Work pairs (typical case:
same author, same normalised title, sim 0.74 — just below the 0.78
floor), we have a recall failure mode that needs its own attention.

This is a *smaller* version of P-29 (missed-merge audit), focused
on the auto-reject band specifically. P-29 takes a broader gold-
driven approach; B here is opportunistic, reusing the bench.

### Deliverable

A short writeup at
`docs/performance/<date>-m6-baseline-audit.md`:
- Agreement matrix (sub-audit A).
- Sub-audit B sample size + flagged-recall-failure count.
- Recommendation: *ship the veto stack* / *harden M6 first* /
  *narrow the veto coverage to only the audit classes M6 handles*.
- Estimated M6 wall on the full corpus post-veto-stack (extrapolate
  from sub-audit A's per-pair latency × projected escalation count).

## Phases

**A.1 Audit script.** New file `scripts/audit-m6-verdicts.py`.
Reads `judge-decisions.jsonl` + `judge-cache.sqlite` + the BFFI
Turtle store; for each pair, runs the audit script's classification
heuristics and emits a precision row (for `same_work` verdicts) and
a recall row (for `different_work` verdicts). Reuses
`audit-merge-clusters.py`'s classification module — no new
heuristic logic, just a different driver. ~80 lines.

**A.2 Run on the 988 bench verdicts.** Local; no LLM calls; expect
~10 s wall. Output: `scratchpad/m6-verdict-audit/{precision,recall}.jsonl`.

**A.3 M6-vs-M5 disagreement scatter** (sub-audit C). One-pass over
`judge-decisions.jsonl` joining M5 similarity ↔ M6 verdict ↔ audit
verdict. Output: `scratchpad/m6-verdict-audit/m6-vs-m5.csv` and a
Markdown snapshot at `docs/performance/<date>-m6-verdict-audit.md`.

**A.4 Recommendation writeup.** Same snapshot. Either:
- *Veto stack ships unchanged* — M6 FP rate substantially below
  M5's, M6 FN rate small enough to ignore.
- *Veto stack ships with P-21 acceleration* — M6's `same_work`
  hallucinations cluster on the P-21 pattern; harden the prompt
  first, then ship the rest of the stack.
- *Veto stack narrows* — M6 disagrees with the heuristic too often
  on certain audit classes; those vetoes should keep auto-merging
  instead of escalating, since M6 can't be relied on to catch
  them.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (completed 2026-05-14; see ../completed/), and P-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove this very proposal around a false premise; the audit-against-bench-data approach here only works if those bench numbers are themselves trustworthy. See [`P-30`](p-30-observability-audit-trail-critical-audit.md).
- The bench artefacts at `scratchpad/overnight-sample-2026-05-13/`:
  `judge-decisions.jsonl` (1342 rows), `judge-cache.sqlite` (988
  M6 verdicts), `bffi/` (BFFI Turtle for context lookups),
  `marcxml/` (source records).
- The audit baseline at
  `scratchpad/merge-cluster-verdicts/verdicts.jsonl`.
- No LLM access required — the verdicts are already on disk.

## Risks

- **R1 — Audit heuristics aren't ground truth.** The audit script
  is a deterministic classifier with known limits (96 % agreement
  with manual inspection in P-26's run). When audit and M6
  disagree, sometimes M6 is right. Mitigation: spot-check ≥ 10
  disagreements by hand per audit class before reading the matrix
  as scripture; the matrix is a *signal*, not a verdict.
- **R2 — Bench coverage bias.** The 988 M6 verdicts come from
  whatever pairs M5's blocker generated *and* whose similarity fell
  in the escalate band on this bench's content mix. Different
  Helmet corpora (music-heavy, archive-heavy, ...) may produce
  different escalate-band populations and different M6 quality
  signals. Mitigation: state the caveat in the writeup; treat
  numbers as bench-specific, not corpus-universal.
- **R3 — Observability ambiguity already present.** This proposal
  itself was almost drafted around the wrong premise (`used_cascade`
  misread). Future audits and dashboards should treat the cache row
  count as the authoritative "M6 fired" signal, not the
  `judge-decisions.jsonl` boolean. Fold this into P-17 (sidecar
  / observability surface review) if it isn't already covered.
- **R4 — Stale audit-class definitions.** The heuristic classifier
  was iterated against the 183 merge clusters; the 988 M6 verdicts
  are a different population (pair-level, not cluster-level). New
  patterns may surface that the heuristic doesn't classify. Tag
  them as `uncertain` and inspect by hand; the M6-verdict audit's
  uncertain-class rate is itself a finding (M6 may be doing
  something the heuristic doesn't model).

## Open questions

- Should sub-audit B (auto-reject recall) live in this proposal or
  fold into P-29? They're related but answer different questions.
  Keep here as a small first-pass; P-29 is the full recall
  audit with a gold set.
- Does M6 need its own confidence calibration before the veto stack
  lands? Currently M6 reports confidence ∈ [0, 1] but we don't know
  if 0.8 means "8 out of 10 such verdicts are correct" or just "the
  model is confident". The force-fire run provides the data to fit
  a confidence-vs-accuracy curve.
- Does sub-audit A double-serve P-21? P-21 was motivated by a
  single M6 hallucination; sub-audit A would tell us whether that's
  systemic or isolated. If isolated, P-21 can ship as-is. If
  systemic across the `subtitle_divergence` class, P-21's prompt-
  hardening needs to expand.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `scripts/audit-m6-verdicts.py` exists; reuses
      `audit-merge-clusters.py`'s classification module.
- [ ] Precision matrix: M6 FP rate measured on the 83 `same_work`
      verdicts, broken down by audit class.
- [ ] Recall matrix: M6 FN rate measured on the 905
      `different_work` verdicts, broken down by audit class.
- [ ] M6-vs-M5 disagreement scatter: similarity vs M6 verdict vs
      audit verdict for all 988 escalate-band pairs.
- [ ] ≥ 10 disagreements per audit class spot-checked by hand;
      consensus recorded in the writeup.
- [ ] `docs/performance/<date>-m6-verdict-audit.md` snapshot
      committed.
- [ ] Decision recorded: ship the veto stack as-is / accelerate
      P-21 first / narrow specific vetoes.

## What this proposal does NOT do

- Doesn't ship any pipeline code changes (it's an audit + decision).
- Doesn't redesign M6's prompt (that's P-21's territory, and
  the audit may motivate expanding P-21).
- Doesn't replace `make eval`'s gold-set evaluation — that's a
  fixed-fixture quality bar; this is a corpus-derived calibration.
- Doesn't establish a recurring audit cadence — P-28 codifies
  that.

## Composition with sibling proposals

- **Gates the veto stack.** P-20 / 23 / 24 / 25 / 26 all assume
  M6 catches cases they escalate. With M6 already firing 988
  times/bench, P-27's matrices turn that assumption from
  unmeasured into measured. Recommend graduating P-27 *before*
  the veto stack.
- **Quantifies P-21's importance.** P-21 hardens M6's prompt
  against a specific hallucination class. P-27 measures whether
  that class is one stray case or a sizeable share of M6's 83
  `same_work` verdicts. If the latter, P-21 must ship before
  (or alongside) the veto stack.
- **Cross-checks P-26's 51 % FP claim.** P-26 attributed the
  FP rate to M5 alone. P-27 disaggregates: how much of the 51 %
  came from M5's 354 auto-merges vs M6's 83 `same_work` verdicts?
  Could reshape P-26's motivation (and the veto-stack's value
  proposition).
- **Feeds P-28.** Once P-27 produces an M6 baseline, P-28
  can pin the agreement matrices as a regression target.
- **Overlaps with P-29's sub-audit B was**. P-29 originally
  proposed sampling auto-rejects for FNs; P-27 sub-audit B does
  this for the M6 `different_work` verdicts instead. Adjust P-29's
  sub-audit B scope accordingly when P-29 ships.
- **Feeds P-17.** The `used_cascade` misread documented in the
  Motivation is itself an observability finding — fold into P-17's
  exporter / sidecar review as a "flag-naming gotcha" data point.

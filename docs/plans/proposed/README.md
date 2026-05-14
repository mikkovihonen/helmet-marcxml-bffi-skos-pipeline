# Proposed plans

Documents in this folder are **proposals**, not committed work. They
sketch directions we might take but haven't decided on. A proposal
graduates into [`docs/plans/backlog/`](../backlog/) — a thorough plan
of record, with sequenced phases, verification checkpoints, and a
rollback procedure.

A proposal can also be marked `rejected` with a one-line reason and
left in place for the record.

**On graduation** the file is `git mv`'d from `proposed/` into the
matching state sub-folder (`backlog/` / `in-progress/` /
`completed/`) AND its content is rewritten from proposal-shape
(Motivation / Approach / Open questions) into plan-shape (sequenced
phases with verification checkpoints, risk register, rollback
procedure, Plan-base commit, Phase commits). The proposal stays in
`proposed/` only while its status is `proposed` or `rejected`.

History note: under the prior workflow (before 2026-05-14) proposals
used a `prop-<NN>-` filename prefix and were *deleted* on graduation,
with a new `p-<NN>-` plan file created in the destination sub-folder.
The 2026-05-14 convention unifies the filename prefix across
proposed → backlog → in-progress → completed, so graduation is a
single `git mv` + content rewrite; `git log --follow` traces the
file's lineage end-to-end. Completed plans with `Source proposal:
prop-NN-...` fields predate this change and stay as the historical
record.

## File-naming convention

One proposal per file. Filenames follow
`p-<NN>-<slug>.md` — the same shape as plans in `backlog/` /
`in-progress/` / `completed/`. `NN` is a zero-padded sequence number
(continuous across proposals and plans), and `<slug>` is a brief
kebab-case summary of the document's intent
(e.g. `p-01-llm-distillation-pre-screener-for-M6.md`). The H1 heading
inside the file uses `P-<NN>` (uppercase, no prefix) so prose
references like `§ P-01` resolve identically before and after
graduation.

## Template

Each proposal carries these sections:

- **Status** — `proposed` / `rejected (reason)`. Read that first
  before treating anything in here as a plan of record.
- **Scope** — rough size (half-day / 1-2 days / 1-2 weeks / multi-stage).
- **Proposal-base commit** — see "Tying proposals to version
  control" below.
- **Motivation** — what the current pipeline does, and what's
  expensive about it.
- **Approach** — the proposed change, kept high-level.
- **Prerequisites** — what has to be true before we can start.
- **Risks** — what could go wrong, and how we'd notice.
- **Open questions** — anything that should be settled before the
  proposal can graduate. Counterpoints and rejected alternatives
  also live here (with the reasoning) so the trade-off is on the
  record.

## Tying proposals to version control

A proposal is a snapshot. Its "Motivation" and "Approach" reason
about the code as it stood when the proposal was drafted; if `main`
moves a lot before the proposal is acted on, parts can quietly go
stale. To make that drift detectable, each proposal carries a
**Proposal-base commit** field near the top:

- The base commit is the commit the proposal was drafted against
  (usually the commit that introduced the proposal file, or its
  parent if the proposal was reasoning about a state just before
  its own landing).
- **Material updates** are listed as a short bullet list under the
  base — each entry pairs a commit hash with one phrase describing
  what changed in the proposal text (e.g. "added counterpoint", "
  prerequisites tightened"). These give a fast way to scan the
  proposal's intellectual history without `git log -p` archaeology.
- Before acting on a proposal, run `git diff <base>..HEAD --
  <relevant paths>` to confirm the section the proposal touches has
  not been refactored out from under it. The proposal should
  explicitly name those relevant paths.

When a proposal graduates into a plan, the plan picks up its own
**Plan-base commit** field (see [`../README.md`](../README.md)).
The proposal's commit-hash trail and the plan's are independent
records — the proposal documents *when the idea was conceived*; the
plan documents *when the execution was scheduled*.

## Current proposals

### Gating sequence (2026-05-14)

After the 2026-05-13 overnight bench's `used_cascade` near-miss
(see P-27 Motivation), proposals 20-29 are **gated on observability
trustworthiness**: P-17 + P-18 + P-19 implemented AND P-30
(critical audit of observability + audit-trail practices) complete
and signed off. The merge-cluster audit's numbers may all be
load-bearing on observability surfaces that haven't been verified
non-misleading; shipping audit-driven changes before the gate
clears risks repeating the P-27 near-miss.

Operational sequence:

1. **P-17, P-18, P-19** — observability code changes. **Done
   2026-05-14.** Code shipped at `9a0601d` (P-17), `5148746` (P-18 +
   P-19 Phase A), `ec6b35c` (P-19 Phase B). All three plans in
   [`../completed/`](../completed/). P-19 verified with a 25x M8
   corpus-load speedup on the 20 k bench
   (see [`docs/performance/2026-05-14-m8-corpus-load.md`](../../performance/2026-05-14-m8-corpus-load.md));
   P-17 + P-18 completed-by-evidence (unit tests + live event traces
   from the P-19 re-bench substantiate the bench smoke tests).
2. **P-31** — dashboard artifacts panel + per-run cataloguer-review
   TSVs. Graduated to [`../backlog/p-31-dashboard-artifacts-panel.md`](../backlog/p-31-dashboard-artifacts-panel.md) on 2026-05-14;
   ready to execute. Ships *before* P-30 so the audit catalogues
   the dashboard in its final shape rather than auditing a surface
   about to grow.
3. **P-32** — run lifecycle management (manifest + list + prune +
   tagging CLI). Surfaced during P-31 review (per-run TSV
   accumulation is a symptom of the wider run-on-disk problem).
   Composes with P-31 — operator hygiene for the per-run TSVs lives
   here. Ships *before* P-30 so the new run-manifest surface is in
   the audit's truth-table catalogue.
4. **P-30** — critical audit + truth-table sign-off. Audits the
   P-31 + P-32 additions as part of its catalogue.
5. **P-20 through P-29** — unblocked once gate (4) clears.

### Proposals

- [`p-32-run-lifecycle-management.md`](p-32-run-lifecycle-management.md)
  — `proposed`. Surfaced during P-31 review (the per-run TSV
  accumulation R4 framing made clear the underlying issue is wider
  than just TSVs). Each pipeline run drops 25-50 GB of artifacts at
  full-corpus scale; today the operator manages run accumulation
  by hand-tracking which `BFFI_DATA_DIR` belongs to which bench
  and `rm -rf`ing the rest. Five failure modes: no registry of
  what runs exist, no "delete older than X" pattern, no
  tag-protection concept, **no canonical location** (runs land
  wherever `BFFI_DATA_DIR` was pointed), and **no way to keep
  Prometheus + the dashboard in sync with on-disk reality after
  prune**. Seven phases: A `bffi-run.json` per-run manifest, B
  `runs list`, C `runs prune` (--dry-run default, --apply required,
  refuses to delete without a filter that excludes some runs),
  D `runs tag` / `untag` / `info`, **E canonical
  `<BFFI_RUNS_ROOT>/<run_uuid>/` invariant for new runs** (deprecates
  operator-picked `BFFI_DATA_DIR`), **F one-time `runs migrate`
  command** that sweeps legacy run-shaped dirs out of `scratchpad/`
  / `data/` into the canonical root with synthesised manifests,
  **G `prune --reset-exporter --reset-prometheus`** that restarts
  the metrics exporter (drops stale in-memory series) and calls
  Prometheus's TSDB admin API to delete the pruned run's series
  from disk. Sequences before P-30 so all new surfaces (manifest,
  CLI, canonical-root invariant, reset machinery) are in the audit
  truth-table.
- [`p-30-observability-audit-trail-critical-audit.md`](p-30-observability-audit-trail-critical-audit.md)
  — `proposed`. Triggered by the 2026-05-13 `used_cascade` near-
  miss. Catalogues every observability + audit-trail surface
  (stage-events, judge-decisions, judge-cache, PROV-O graph,
  `bffi:adminMetadata`, Grafana panels, CLI counters), specs ground-
  truth meaning per surface, runs drift checks, produces
  `docs/observability-truth-table.md` as authoritative consumer-
  facing reference. **Gates proposals 20-29.** Sequenced after
  P-17/18/19 (auditing surfaces about to be reshaped is wasted
  work). Out-of-scope: fixing every drift it surfaces — fixes
  become P-31+ remediation work.
- [`p-01-llm-distillation-pre-screener-for-M6.md`](p-01-llm-distillation-pre-screener-for-M6.md)
  — `proposed`. Distil M6's structured LLM verdicts into a cheap
  classifier that short-circuits the obvious pairs on subsequent
  batches.
- [`p-05-anonymous-work-canonicalisation.md`](p-05-anonymous-work-canonicalisation.md)
  — `proposed`. M8 currently mints canonical Works only when a MARC
  100/110 → URI agent → prefLabel chain exists, sending the rest to
  `canonical-conflicts.jsonl`. Proposes a fallback URI-minting policy
  for anonymous / secondary-creator-only records, with three options
  of increasing ambition.
- [`p-06-structured-output-backend.md`](p-06-structured-output-backend.md)
  — `proposed`. P-02 A5 found mlx-lm 0.31 has no constrained decoding
  for `response_format: json_schema`; the fix landed at the prompt
  layer via `src/bffi_pipeline/llm_json_mode.py`. This proposal
  weighs three server-side alternatives (outlines wrapper, vllm-mlx,
  fork mlx-lm) for the case where the prompt-layer approach proves
  insufficient. Stays `proposed` unless a concrete incident motivates
  action.
- [`p-07-bibframe-856-as-item.md`](p-07-bibframe-856-as-item.md)
  — `proposed`. marc2bibframe2 lifts MARC 856 (Electronic Location
  and Access) as a separate `bf:Instance`, which is semantically
  closer to `bf:Item` for the typical Helmet usage. This proposal
  sketches three depth levels for the semantic fix — local M2
  rewrite, configurable per-856 classifier, or an upstream PR to
  marc2bibframe2 — and documents what would have to be true for it
  to be worth shipping.
- [`p-09-library-agnostic-source.md`](p-09-library-agnostic-source.md)
  — `proposed`. Decouple `bffi_pipeline` from FI-HELME so the
  downstream stages can serve any Finnish library whose export tool
  emits MARCXML with a populated controlfield 003. Phase A reads
  `bib_id` from MARC 001 instead of the filename stem; Phase B pulls
  the nine-site FI-HELME URI cluster into a config-driven
  `LibrarySource` registry keyed on MARC 003.
- [`p-21-m6-translation-hallucination-mitigation.md`](p-21-m6-translation-hallucination-mitigation.md)
  — `proposed`. Sibling of P-20. A SECOND false-positive merge
  on the 2026-05-13 overnight run — b23008490 ("Alvar Aalto :
  taide ja moderni muoto" 2017, fin) and b24731298 ("Alvar Aalto :
  Maison Louis Carré" 2018, fre) — escalated correctly out of
  M5 (sim 0.844 < 0.90) but **M6's LLM judge said `same_work`
  at conf 0.95**, hallucinating an RDA translation relationship.
  Two compounding layers: the LLM over-applied translation
  inference, AND both records put deceased Aalto in MARC 100
  (Helmet's subject-of-art-book cataloguing convention).
  Proposes three phases — A: prompt hardening (~half day);
  B: M2/M3 100-as-subject demotion via deceased-person + posthumous
  -publication-window detection (1-2 days); C: post-pick
  corroborating-signal validation at M6 (1 day). Composes with
  P-20 (M5 layer) and P-16 (fallback gating).
- [`p-20-auto-merge-false-positive-mitigation.md`](p-20-auto-merge-false-positive-mitigation.md)
  — `proposed`. M5's auto-merge band (sim ≥ 0.90 → ``same_work``
  without M6 LLM verification) caught a false positive on the
  2026-05-13 overnight run: b1499110x ("Alvar Aalto :
  mestariteoksia" 1998) and b18086238 ("Alvar Aalto : his life"
  2007) — distinct books in Schildt's Aalto bibliography — merged
  as one canonical Work at similarity 0.9061. Root cause: M3 drops
  the 245$b subtitle from ``skos:prefLabel``, so the embedding
  input string has identical title fields. Proposes B + C: include
  subtitle in a new ``bffi:fullTitle`` for the embedding vector,
  plus a year-distance veto (≥ 5 yr → escalate to M6) as a
  belt-and-braces safety net. A (tighten 0.90 → 0.95 threshold)
  and D (disable auto-merge) stay as future rollback knobs.
- [`p-22-m5-same-author-title-overlap-floor.md`](p-22-m5-same-author-title-overlap-floor.md)
  — `proposed` *(likely superseded by P-26 — see P-26's "Why
  this might supersede P-22")*. Largest false-positive class on
  the 2026-05-13 overnight bench: **40 / 183 (21.9 %)** merges
  collapsed distinct Works by the same author (children's series,
  detective series, catalogs). Author dominates the embedding;
  series prefix carries the rest. Proposes a stopword-filtered
  substantive-token *overlap floor* at the M5 auto-merge band:
  same-author pairs sharing < 3 substantive title tokens demote from
  `auto-merge` to `escalate`. Lifts the audit script's
  `_substantive_tokens` into a shared module so production and audit
  stay aligned. Composes with P-20 / P-23 / P-24 (disjoint
  vetoes that all demote to M6). Kept on the record while P-26 is
  under consideration; mark `rejected (superseded by P-26)` on
  P-26 graduation.
- [`p-27-m6-llm-judge-baseline-audit.md`](p-27-m6-llm-judge-baseline-audit.md)
  — `proposed`. The 2026-05-13 overnight bench fired **988 M6 LLM
  judge calls** (verified against `judge-cache.sqlite`, not the
  misleading `used_cascade: false` flag in
  `judge-decisions.jsonl`). Bench distribution: 354 M5 auto-merges
  + 988 M6-judged escalations (83 `same_work` + 905
  `different_work`). M6 is already a major contributor to the 51 %
  FP rate the merge-cluster audit measured — the audit was measuring
  M5 + M6 combined, not M5 alone. Proposes auditing the existing
  988 verdicts (no LLM re-firing needed) against the heuristic
  classifier: precision matrix on the 83 `same_work` rows,
  recall matrix on the 905 `different_work` rows, M6-vs-M5
  similarity scatter on all 988. Decides whether the veto stack
  ships as-is, accelerates P-21 first, or narrows specific
  vetoes whose escalation targets M6 can't reliably catch.
  **Gates the veto stack.** Also surfaces a `used_cascade` flag-
  naming gotcha for P-17 to absorb.
- [`p-28-audit-as-ci-regression-fixture.md`](p-28-audit-as-ci-regression-fixture.md)
  — `proposed`. `scripts/audit-merge-clusters.py` is unrecognised
  regression infrastructure: 96 % verdict coverage, deterministic,
  no LLM / network dependency — exactly what CI wants. Today it
  runs by hand and its verdict distribution isn't asserted anywhere.
  Proposes pinning the 20 k bench's merge-cluster slice as
  `tests/fixtures/audit-bench-2026-05-13/` (MARCXML + canonical-map
  + expected-verdicts, ~1.2 MB), adding a `make audit-test` target
  that compares actual vs expected verdicts, and CI-triggering on
  PRs that touch M5 / M6 / M8 / SPARQL / `text/`. Turns every veto
  PR's "audit-FP rows must escalate" criterion from aspirational
  markdown into an enforceable CI gate. Periodic full-corpus
  re-bench stays manual (5 h wall, not in CI). **Enables every
  veto proposal's regression criteria.**
- [`p-29-m5-missed-merge-recall-audit.md`](p-29-m5-missed-merge-recall-audit.md)
  — `proposed`. Every audit on the board (P-22 / 23 / 24 / 25 /
  26 / 27) measures the false-positive surface (what merged that
  shouldn't have). The **false-negative surface** is invisible:
  pairs that *should* have merged but didn't — auto-rejected, never
  even candidate-generated, or cross-block-missed. Estimated low
  thousands of missed merges on the 800 k corpus, of comparable
  magnitude to the FP surface but unmeasured. Proposes a three-
  phase recall audit: A bootstrap a gold set from existing same-
  work groups' transitivity (zero cataloguer time), B authority-
  driven gold pairs from KANTO/VIAF URIs, C cataloguer-pinned
  residual cases. Output: recall@candidate and recall@blocker
  metrics + a top-3 failure-mode breakdown. Pure measurement
  proposal — fixes (e.g. blocker broadening) are downstream work.
- [`p-26-m5-symdiff-idf-title-discrimination.md`](p-26-m5-symdiff-idf-title-discrimination.md)
  — `proposed`. Information-theoretic refinement that supersedes
  P-22. In two near-identical titles, the shared words
  discriminate nothing; the *symmetric difference*, weighted by
  corpus-derived IDF, carries essentially all of the same-Work
  signal. Phase A builds a one-time IDF table at M3 finalisation
  from `bffi:fullTitle` and persists it as
  `<BFFI_DATA_DIR>/title-token-idf.parquet` (~5 MB, ~1 min on 800 k
  given P-19's concat file). Phase B reads the table at M5
  startup and demotes auto-merge → escalate when the discriminator
  mass `sum(idf[t] for t in title_a ⊕ title_b)` exceeds a calibrated
  threshold. Self-tuning per language: a Swedish common word ("att")
  naturally lands in the bottom IDF tier of the Swedish title
  sub-corpus — no hand-curated stopword list to maintain across
  Estonian / Polish / Korean as the corpus expands. Cold-start
  fallback is a boolean symdiff check (over-escalates short
  re-editions, but never under-escalates). Strictly dominates
  P-22 on same-author FPs, anonymous-work FPs, short-title FEs,
  and multilingual coverage; P-22 marked `rejected (superseded
  by P-26)` on P-26 graduation.
- [`p-23-m5-numeric-marker-veto.md`](p-23-m5-numeric-marker-veto.md)
  — `proposed`. Second-largest false-positive class: **37 / 183
  (20.2 %)** merges flatten records that differ on a numeric marker
  embedded in the title — volume numbers (Vol. 2 vs Vol. 11, ES 284 vs
  ES 295, Bach Kantatenwerk 21/26/30) or in-title years (Live at
  Montreux 1990 vs 2010, Vuoden 1992 vs 2003 valtiopäivät). Proposes
  lifting the audit's `_VOLUME_PATTERNS` + `_years_in_title` into
  `src/bffi_pipeline/text/markers.py` and adding a veto at the M5
  auto-merge band: pair with mismatching volume or in-title years
  escalates to M6.
- [`p-24-m5-distinct-author-veto.md`](p-24-m5-distinct-author-veto.md)
  — `proposed`. Smallest count but qualitatively worst-case: **2 / 183
  (1.1 %)** merges span distinct MARC-100 authors. Proposes a single-
  line veto at the M5 auto-merge band — same-band pairs with both
  records carrying non-empty `creator:` AND `norm_author(a) !=
  norm_author(b)` demote to `escalate`. NFKD-strip + casefold +
  alphanumeric-only normaliser handles diacritic variants. Lifts
  `_norm_author` to a shared `text/normalize.py` module.
- [`p-25-m5-anthology-scope-veto.md`](p-25-m5-anthology-scope-veto.md)
  — `proposed`. **2 / 183 (1.1 %)** merges mixed anthology /
  collected-works titles with specific component works (FRBR scope
  mismatch — an anthology is a distinct Work from its components).
  Proposes lifting `_is_anthology_title` + the marker set into
  `src/bffi_pipeline/text/scope.py` and adding an asymmetric-pattern
  veto: when exactly one record's title contains an anthology marker
  ("complete", "selected", "kootut", "samlade", "œuvres"), demote
  from `auto-merge` to `escalate`.
_(P-15 and P-16 graduated to plans on 2026-05-13 and shipped
in the same session; see
[`../completed/p-15-preserve-authority-uris-at-m3.md`](../completed/p-15-preserve-authority-uris-at-m3.md)
and [`../completed/p-16-fallback-tier-confidence-gating.md`](../completed/p-16-fallback-tier-confidence-gating.md).
P-17, P-18, and P-19 graduated and shipped on 2026-05-14; see
[`../completed/p-17-exporter-multi-sidecar-discovery.md`](../completed/p-17-exporter-multi-sidecar-discovery.md),
[`../completed/p-18-m8-emit-start-before-corpus-load.md`](../completed/p-18-m8-emit-start-before-corpus-load.md),
[`../completed/p-19-m8-corpus-load-throughput.md`](../completed/p-19-m8-corpus-load-throughput.md).
P-31 graduated to backlog on 2026-05-14, ready to execute; see
[`../backlog/p-31-dashboard-artifacts-panel.md`](../backlog/p-31-dashboard-artifacts-panel.md).
First graduation under the unified `p-NN-` naming, done as a
single `git mv` + content rewrite per the new convention.)_

## Graduated / completed / abandoned

When a proposal graduates to a plan, **`git mv` the file** from
`proposed/` to the destination state sub-folder (`backlog/` /
`in-progress/` / `completed/`) in the same commit as the content
rewrite. Because the filename prefix is already `p-` on both sides,
the move preserves the slug; `git log --follow
<destination>/p-<NN>-<slug>.md` traces history through the rename.
The plan's `Source proposal:` field, when present, points at the
pre-graduation commit so a one-line `git show <commit>:<path>`
recovers the proposal-shape text.

Completed plans that graduated under the pre-2026-05-14 workflow
(p-15, p-16, p-17, p-18, p-19) reference their sources as
`prop-<NN>-...` because that's the filename their proposals lived
under at the time. Don't rewrite those references — they're
historically correct and point at git-resolvable commits.

The currently-graduated set is enumerated by reading the plan files
under [`../backlog/`](../backlog/), [`../in-progress/`](../in-progress/),
[`../completed/`](../completed/), and [`../abandoned/`](../abandoned/).

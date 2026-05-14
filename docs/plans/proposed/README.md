# Proposed plans

Documents in this folder are **proposals**, not committed work. They
sketch directions we might take but haven't decided on. A proposal
graduates into [`docs/plans/backlog/`](../backlog/) — a thorough plan
of record, with sequenced phases, verification checkpoints, and a
rollback procedure.

A proposal can also be marked `rejected` with a one-line reason and
left in place for the record.

**On graduation** the source proposal file is deleted (the resulting
plan is the canonical record). The proposal stays in `proposed/`
only while its status is `proposed` or `rejected`. This is a 2026-05-13
convention change from the prior `docs/proposals/` layout, where
graduated proposals lived on as stubs pointing at their plans;
under the new layout the plan's own `Source proposal:` field carries
that history.

## File-naming convention

One proposal per file. Filenames follow
`prop-<NN>-<slug>.md`, where `NN` is a zero-padded sequence number
and `<slug>` is a brief kebab-case summary of the proposal's intent
(e.g. `prop-01-llm-distillation-pre-screener-for-M6.md`). The H1
heading inside the file uses `P-<NN>` (no `prop-` prefix) so that
existing references like `§ P-01` and `§ P-02` keep resolving in
prose.

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
(see prop-27 Motivation), proposals 20-29 are **gated on observability
trustworthiness**: P-17 + P-18 + P-19 implemented AND prop-30
(critical audit of observability + audit-trail practices) complete
and signed off. The merge-cluster audit's numbers may all be
load-bearing on observability surfaces that haven't been verified
non-misleading; shipping audit-driven changes before the gate
clears risks repeating the prop-27 near-miss.

Operational sequence:

1. **P-17, P-18, P-19** — observability code changes. *Graduated to plans
   on 2026-05-14; code-side phases shipped at `9a0601d` (P-17) and
   `5148746` (P-18 + P-19). Bench / smoke verification pending — see the
   plans under [`../in-progress/`](../in-progress/).*
2. **prop-30** — critical audit + truth-table sign-off.
3. **prop-20 through prop-29** — unblocked once gate (2) clears.

### Proposals

- [`prop-30-observability-audit-trail-critical-audit.md`](prop-30-observability-audit-trail-critical-audit.md)
  — `proposed`. Triggered by the 2026-05-13 `used_cascade` near-
  miss. Catalogues every observability + audit-trail surface
  (stage-events, judge-decisions, judge-cache, PROV-O graph,
  `bffi:adminMetadata`, Grafana panels, CLI counters), specs ground-
  truth meaning per surface, runs drift checks, produces
  `docs/observability-truth-table.md` as authoritative consumer-
  facing reference. **Gates proposals 20-29.** Sequenced after
  prop-17/18/19 (auditing surfaces about to be reshaped is wasted
  work). Out-of-scope: fixing every drift it surfaces — fixes
  become prop-31+ remediation work.
- [`prop-01-llm-distillation-pre-screener-for-M6.md`](prop-01-llm-distillation-pre-screener-for-M6.md)
  — `proposed`. Distil M6's structured LLM verdicts into a cheap
  classifier that short-circuits the obvious pairs on subsequent
  batches.
- [`prop-05-anonymous-work-canonicalisation.md`](prop-05-anonymous-work-canonicalisation.md)
  — `proposed`. M8 currently mints canonical Works only when a MARC
  100/110 → URI agent → prefLabel chain exists, sending the rest to
  `canonical-conflicts.jsonl`. Proposes a fallback URI-minting policy
  for anonymous / secondary-creator-only records, with three options
  of increasing ambition.
- [`prop-06-structured-output-backend.md`](prop-06-structured-output-backend.md)
  — `proposed`. P-02 A5 found mlx-lm 0.31 has no constrained decoding
  for `response_format: json_schema`; the fix landed at the prompt
  layer via `src/bffi_pipeline/llm_json_mode.py`. This proposal
  weighs three server-side alternatives (outlines wrapper, vllm-mlx,
  fork mlx-lm) for the case where the prompt-layer approach proves
  insufficient. Stays `proposed` unless a concrete incident motivates
  action.
- [`prop-07-bibframe-856-as-item.md`](prop-07-bibframe-856-as-item.md)
  — `proposed`. marc2bibframe2 lifts MARC 856 (Electronic Location
  and Access) as a separate `bf:Instance`, which is semantically
  closer to `bf:Item` for the typical Helmet usage. This proposal
  sketches three depth levels for the semantic fix — local M2
  rewrite, configurable per-856 classifier, or an upstream PR to
  marc2bibframe2 — and documents what would have to be true for it
  to be worth shipping.
- [`prop-09-library-agnostic-source.md`](prop-09-library-agnostic-source.md)
  — `proposed`. Decouple `bffi_pipeline` from FI-HELME so the
  downstream stages can serve any Finnish library whose export tool
  emits MARCXML with a populated controlfield 003. Phase A reads
  `bib_id` from MARC 001 instead of the filename stem; Phase B pulls
  the nine-site FI-HELME URI cluster into a config-driven
  `LibrarySource` registry keyed on MARC 003.
- [`prop-21-m6-translation-hallucination-mitigation.md`](prop-21-m6-translation-hallucination-mitigation.md)
  — `proposed`. Sibling of prop-20. A SECOND false-positive merge
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
  prop-20 (M5 layer) and prop-16 (fallback gating).
- [`prop-20-auto-merge-false-positive-mitigation.md`](prop-20-auto-merge-false-positive-mitigation.md)
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
- [`prop-22-m5-same-author-title-overlap-floor.md`](prop-22-m5-same-author-title-overlap-floor.md)
  — `proposed` *(likely superseded by prop-26 — see prop-26's "Why
  this might supersede prop-22")*. Largest false-positive class on
  the 2026-05-13 overnight bench: **40 / 183 (21.9 %)** merges
  collapsed distinct Works by the same author (children's series,
  detective series, catalogs). Author dominates the embedding;
  series prefix carries the rest. Proposes a stopword-filtered
  substantive-token *overlap floor* at the M5 auto-merge band:
  same-author pairs sharing < 3 substantive title tokens demote from
  `auto-merge` to `escalate`. Lifts the audit script's
  `_substantive_tokens` into a shared module so production and audit
  stay aligned. Composes with prop-20 / prop-23 / prop-24 (disjoint
  vetoes that all demote to M6). Kept on the record while prop-26 is
  under consideration; mark `rejected (superseded by prop-26)` on
  prop-26 graduation.
- [`prop-27-m6-llm-judge-baseline-audit.md`](prop-27-m6-llm-judge-baseline-audit.md)
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
  ships as-is, accelerates prop-21 first, or narrows specific
  vetoes whose escalation targets M6 can't reliably catch.
  **Gates the veto stack.** Also surfaces a `used_cascade` flag-
  naming gotcha for prop-17 to absorb.
- [`prop-28-audit-as-ci-regression-fixture.md`](prop-28-audit-as-ci-regression-fixture.md)
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
- [`prop-29-m5-missed-merge-recall-audit.md`](prop-29-m5-missed-merge-recall-audit.md)
  — `proposed`. Every audit on the board (prop-22 / 23 / 24 / 25 /
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
- [`prop-26-m5-symdiff-idf-title-discrimination.md`](prop-26-m5-symdiff-idf-title-discrimination.md)
  — `proposed`. Information-theoretic refinement that supersedes
  prop-22. In two near-identical titles, the shared words
  discriminate nothing; the *symmetric difference*, weighted by
  corpus-derived IDF, carries essentially all of the same-Work
  signal. Phase A builds a one-time IDF table at M3 finalisation
  from `bffi:fullTitle` and persists it as
  `<BFFI_DATA_DIR>/title-token-idf.parquet` (~5 MB, ~1 min on 800 k
  given prop-19's concat file). Phase B reads the table at M5
  startup and demotes auto-merge → escalate when the discriminator
  mass `sum(idf[t] for t in title_a ⊕ title_b)` exceeds a calibrated
  threshold. Self-tuning per language: a Swedish common word ("att")
  naturally lands in the bottom IDF tier of the Swedish title
  sub-corpus — no hand-curated stopword list to maintain across
  Estonian / Polish / Korean as the corpus expands. Cold-start
  fallback is a boolean symdiff check (over-escalates short
  re-editions, but never under-escalates). Strictly dominates
  prop-22 on same-author FPs, anonymous-work FPs, short-title FEs,
  and multilingual coverage; prop-22 marked `rejected (superseded
  by prop-26)` on prop-26 graduation.
- [`prop-23-m5-numeric-marker-veto.md`](prop-23-m5-numeric-marker-veto.md)
  — `proposed`. Second-largest false-positive class: **37 / 183
  (20.2 %)** merges flatten records that differ on a numeric marker
  embedded in the title — volume numbers (Vol. 2 vs Vol. 11, ES 284 vs
  ES 295, Bach Kantatenwerk 21/26/30) or in-title years (Live at
  Montreux 1990 vs 2010, Vuoden 1992 vs 2003 valtiopäivät). Proposes
  lifting the audit's `_VOLUME_PATTERNS` + `_years_in_title` into
  `src/bffi_pipeline/text/markers.py` and adding a veto at the M5
  auto-merge band: pair with mismatching volume or in-title years
  escalates to M6.
- [`prop-24-m5-distinct-author-veto.md`](prop-24-m5-distinct-author-veto.md)
  — `proposed`. Smallest count but qualitatively worst-case: **2 / 183
  (1.1 %)** merges span distinct MARC-100 authors. Proposes a single-
  line veto at the M5 auto-merge band — same-band pairs with both
  records carrying non-empty `creator:` AND `norm_author(a) !=
  norm_author(b)` demote to `escalate`. NFKD-strip + casefold +
  alphanumeric-only normaliser handles diacritic variants. Lifts
  `_norm_author` to a shared `text/normalize.py` module.
- [`prop-25-m5-anthology-scope-veto.md`](prop-25-m5-anthology-scope-veto.md)
  — `proposed`. **2 / 183 (1.1 %)** merges mixed anthology /
  collected-works titles with specific component works (FRBR scope
  mismatch — an anthology is a distinct Work from its components).
  Proposes lifting `_is_anthology_title` + the marker set into
  `src/bffi_pipeline/text/scope.py` and adding an asymmetric-pattern
  veto: when exactly one record's title contains an anthology marker
  ("complete", "selected", "kootut", "samlade", "œuvres"), demote
  from `auto-merge` to `escalate`.
_(prop-15 and prop-16 graduated to plans on 2026-05-13 and shipped
in the same session; see [`../completed/p-15-preserve-authority-uris-at-m3.md`](../completed/p-15-preserve-authority-uris-at-m3.md)
and [`../completed/p-16-fallback-tier-confidence-gating.md`](../completed/p-16-fallback-tier-confidence-gating.md).
prop-17, prop-18, and prop-19 graduated to plans on 2026-05-14
and their code-side phases shipped in the same session; see
[`../in-progress/p-17-exporter-multi-sidecar-discovery.md`](../in-progress/p-17-exporter-multi-sidecar-discovery.md),
[`../in-progress/p-18-m8-emit-start-before-corpus-load.md`](../in-progress/p-18-m8-emit-start-before-corpus-load.md),
[`../in-progress/p-19-m8-corpus-load-throughput.md`](../in-progress/p-19-m8-corpus-load-throughput.md).
Each has an operator-side bench / smoke-test phase still pending.)_

## Graduated / completed / abandoned

When a proposal graduates to a plan, **delete** the proposal file —
the resulting `p-<NN>-...md` plan under `backlog/` / `in-progress/`
/ `completed/` is the canonical record from that point. The plan's
own `Source proposal:` field preserves the link backwards (proposal
title + original status + the commit the proposal lived at, so
`git show <commit>:docs/proposals/prop-<NN>-<slug>.md` recovers the
text).

The currently-graduated set is enumerated by reading the plan files
under [`../backlog/`](../backlog/), [`../in-progress/`](../in-progress/),
[`../completed/`](../completed/), and [`../abandoned/`](../abandoned/).

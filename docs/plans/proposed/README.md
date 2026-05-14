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
- [`prop-18-m8-emit-start-before-corpus-load.md`](prop-18-m8-emit-start-before-corpus-load.md)
  — `proposed`. The dashboard shows M8 as `pending` for ~8 minutes
  after M6 ends, despite M8's Python process actively loading the
  BFFI corpus the whole time. Move the M8 ``start`` event to the
  top of ``merge.run()`` and add a ``phase_boundary`` once
  ``len(groups)`` is known. ~5 lines.
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
- [`prop-19-m8-corpus-load-throughput.md`](prop-19-m8-corpus-load-throughput.md)
  — `proposed`. M8's corpus-load phase reads 19 570 individual
  BFFI Turtle files via rdflib at ~8 min wall on 20 k records;
  linearly extrapolating to ~5.5 h on the 800 k corpus. Proposes
  layering a single ``bffi-corpus.ttl`` stream over the
  per-record `.ttl` store. Trade-offs across four options
  (concat file, multi-proc pool, bespoke streaming parser,
  status quo); recommendation is Option A (concat file written
  at M3 finalisation) as the smallest-surface largest-win
  first step.
- [`prop-17-exporter-multi-sidecar-discovery.md`](prop-17-exporter-multi-sidecar-discovery.md)
  — `proposed`. The metrics exporter resolves its sidecar path AND
  its error-spec paths at process startup from `BFFI_DATA_DIR` /
  `BFFI_OBSERVABILITY_SIDECAR` — both as separate gotchas. A
  pipeline run against a different `BFFI_DATA_DIR` silently writes
  events + error rows to files the exporter doesn't watch. Surfaced
  by the 2026-05-13 overnight bench launch (5-hour observability
  blackout for the stage events; the M2+M3 failure-mode bargauge
  silently empty for another ~30 min after the sidecar fix).
  Proposes a repeatable `--sidecar`, a `--watch-glob` for
  auto-discovery, per-sidecar error-spec derivation from the
  sidecar's parent dir, and a startup echo of the resolved set.
  No default-behaviour change.

_(prop-15 and prop-16 graduated to plans on 2026-05-13 and shipped
in the same session; see [`../completed/p-15-preserve-authority-uris-at-m3.md`](../completed/p-15-preserve-authority-uris-at-m3.md)
and [`../completed/p-16-fallback-tier-confidence-gating.md`](../completed/p-16-fallback-tier-confidence-gating.md).)_

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

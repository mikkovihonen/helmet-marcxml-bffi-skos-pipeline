# P-57 — Conversion-first pipeline rewrite on the `rewrite` branch

**Status**: proposed (2026-06-10). The `rewrite` branch is cut from the same commit that introduces this proposal.

**Scope**: a focused reimplementation covering MARCXML export, marc2bibframe (LoC XSLT), BIBFRAME → BFFI conversion, and an evaluation harness — running cleanly over the full ~800 k-record Helmet corpus. **No clustering, no reconciliation.** M5 (embeddings), M6 (LLM judge), M7-M9 (reconciliation) are explicitly out of scope for the rewrite; if Skosmos display is needed for evaluation it lands as a passive viewer, not an enrichment surface.

**Driver**: the conversion side (MARCXML → BIBFRAME → BFFI) is the foundation everything else builds on. Recent diagnostics — the 20 k-record run killed on b10007428's 100 × MARC 730 → 200 `bf:Hub` cross-product blow-up, the p-56 hard-cut transition committing the codebase to zero `bf:*` in the BFFI emit — point at structural decisions in the conversion layer that are easier to bake into a fresh implementation than to retrofit. The existing pipeline carries six months of accreted reconciliation logic on top of a conversion layer that hasn't been corpus-scale-validated. A conversion-first rewrite gets the foundation right before re-investing in the downstream stages.

## In / out of scope

### In scope (rewrite branch)
- **MARCXML export** — input from the Helmet Sierra dump (`/Users/mikkovihonen/Workspace/helmet-sierra-data-tools/output/marcxml/` per existing memory). Per-record file layout matches the existing corpus.
- **marc2bibframe** — the LoC XSLT (`third_party/marc2bibframe2/` submodule) wrapped by a thin driver. Behavior unchanged from main.
- **BIBFRAME → BFFI** — SPARQL CONSTRUCT (or equivalent RDF processing) reading the marc2bibframe output and emitting BFFI-only canonical Turtle. Bakes in p-56 from day 1: zero `bf:*` in the emit, every routing per `docs/bf_to_bffi_mapping.md`.
- **Evaluation harness** — round-trip diff (`canonical.ttl → MARCXML → canonical.ttl`) at corpus scale, cataloguer-review HTML, mapping-discipline tests (closed-namespace regression, marcKey-bypass counts).

### Out of scope (stays on main)
- M5 embeddings, M6 LLM judge, M7-M9 reconciliation (KANTO / Finto / YSO / KAUNO / VIAF).
- M10 Skosmos load if it requires enrichment from authority lookups. Passive Skosmos display of the BFFI emit (no reconciliation) is acceptable as an eval surface if it adds value over the cataloguer-review HTML.
- The judge prompts (`prompts/`), the picker tabs, the universal-agent-page work (P-55), the legacy-vocab bridging (P-44).

### What carries over from main as-is

- `docs/lkd.rdf` (BFFI ontology — vendored).
- `docs/bf_to_bffi_mapping.md` (source of truth for every conversion decision).
- `docs/plans/proposed/p-56-...` (hard-cut transition discipline baked in).
- `docs/plans/proposed/p-49-...` (marcKey-bypass diagnostic discipline).
- `third_party/marc2bibframe2/` (LoC XSLT submodule).
- The CLAUDE.md conventions: closed BFFI namespace, Turtle prefix discipline, prompts hashed to provenance, SPARQL in `sparql/`, idempotency, type strictness.

### What gets rewritten from spec

- `src/bffi_pipeline/cli.py` and the orchestrator — simpler shape with three stages (export / convert / eval) instead of M1-M10.
- `sparql/bf_to_bffi_*.rq` — start from the main-branch versions as reference but emit `bffi:*` only (no `bf:*` legacy); discriminator routings from p-56 Phase 4 baked in.
- Round-trip converter — read-side reads BFFI predicates only (no `bf:*` typing as routing key).
- Test fixtures — reuse the gold MARCXML inputs from main; rewrite the assertion files to expect BFFI-only emit.

## Branch policy

- The `rewrite` branch is the working line for everything in scope above.
- `main` stays as the legacy reference; commits to main are limited to: (a) documents that pertain to both branches (this proposal, mapping doc updates, lkd.rdf bumps), (b) emergency fixes if anyone runs the legacy pipeline.
- Direct-to-branch commits, per the existing direct-to-main norm. No feature branches off `rewrite` unless an experiment genuinely needs isolation.
- When the rewrite reaches corpus-scale conversion + eval parity, decide: merge `rewrite` → `main` and archive the legacy pipeline, or keep both lines.

## Phasing (broad — detailed plans land as follow-ons)

The rewrite is large enough that committing to specific phase boundaries before the first prototype runs is premature. The rough sequence:

1. **Scaffold the rewrite branch**: minimal repo layout (cli.py, sparql/, third_party/ submodule reference, tests/). Lift the carry-over assets listed above.
2. **MARCXML → BIBFRAME**: wrap marc2bibframe2 with the thin driver. Run on the curated dev sample (13 bibs) end-to-end.
3. **BIBFRAME → BFFI v0**: emit BFFI-only canonical Turtle. p-56 Phase 1 (clean rename) baked in. No discriminator routings yet — emit will fall short on Hub / Identifier-scheme / Title-variant / Series-link until phase 4.
4. **Eval harness v0**: round-trip diff + cataloguer-review HTML. Establish corpus-scale baseline.
5. **p-56 Phase 4 routings**: Hub, Identifier-scheme, Title-variant, Series-link, Audio. Eval harness signals correctness.
6. **p-56 Phase 5 music interim**: bffi:readMarc382 + bffi:musicKey literal collapse against BFFI 1.0.0.
7. **Full corpus run**: 800 k records, end-to-end conversion + eval. Diagnose corpus-scale failure modes (cross-product blow-ups, memory ceilings, throughput).

Each numbered step warrants its own plan once we have signal from the previous one.

## Open questions

1. **Submodule sharing or rewrite-branch copy?** The `third_party/marc2bibframe2/` submodule is identical on main and rewrite. Sharing via git submodule reference (the rewrite branch inherits main's pointer) is the obvious answer; flag if there's reason to vendor a separate snapshot.
2. **Skosmos as eval surface?** The round-trip diff + cataloguer-review HTML covers conversion correctness mechanically. Skosmos display adds visual inspection but requires running the Docker stack. Default to: keep the cataloguer-review HTML as the primary eval surface; add Skosmos only if it shows value during eval-harness work.
3. **Curated dev sample size during rewrite?** The 13-bib sample is enough to validate end-to-end correctness but might not surface corpus-scale failure modes early. Consider sampling a 1 k / 10 k / 100 k progression alongside the dev sample.
4. **Merge or replace?** When rewrite reaches parity on conversion + eval — does it merge into main (preserving the reconciliation stages there), or does it become the new main (with the reconciliation stages reimplemented or imported back later)? Defer until rewrite reaches that point.

## Verification

A successful rewrite is gauged on:

- **Closed-namespace discipline**: zero `bf:*` URIs in any `canonical.ttl` emitted by the rewrite branch. Enforced by the test extension in p-56's verification section.
- **Round-trip parity**: round-trip diff (`MARCXML → BFFI → MARCXML`) shows the same `identical` / `changed` / `lost` / `tag-changed` / `marckey-bypass` distribution as a comparable run on main, **with the marckey-bypass count strictly lower** (Phase 4 routings read BFFI-side predicates, eliminating the bypass on Hub / VariantTitle / Identifier rows).
- **Corpus scale**: 800 k-record full-corpus conversion completes in bounded wall-clock and memory. The b10007428 cross-product class of failure does not appear (verified by including b10007428 + similar high-cardinality records in the eval set).

## Rollback

The rewrite lives on its own branch. If it doesn't work out, the rewrite branch is deleted; main is unaffected. No data migration on either side because canonical Turtle is rebuilt from MARCXML each run on both branches.

## Suggested next step

Cut the `rewrite` branch from this commit. Then on the rewrite branch, start with step 1 (scaffold) and step 2 (MARCXML → BIBFRAME wrapper). The first observable milestone is end-to-end conversion of the 13-bib dev sample with BFFI-only emit and a round-trip diff showing the closed-namespace discipline holding.

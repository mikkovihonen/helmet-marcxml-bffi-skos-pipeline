# P-57 — Conversion-first pipeline rewrite on the `rewrite` branch

**Status**: active (2026-06-10). The `rewrite` branch was cut at `c0a088e` (on `main`); step 1 — scaffold — shipped at `b242261`. Tracking the remaining steps below.

**Imported from `main`**: this plan started life on `main:docs/plans/proposed/p-57-...` and was brought across to the rewrite branch under the new flat `p-NNN-<slug>.md` convention. The main copy stays put as the cross-branch record; the rewrite branch's copy is the working one. Re-import from main when significant scope changes land there.

**Scope**: a focused reimplementation covering MARCXML export, MARC → BIBFRAME (LoC marc2bibframe2 XSLT), BIBFRAME → BFFI conversion, **BFFI → MARC reverse conversion**, and an evaluation harness — running cleanly over the full ~800 k-record Helmet corpus. **Bidirectional conversion: both MARC → BFFI and BFFI → MARC are first-class on this branch.** **No clustering, no reconciliation.** M5 (embeddings), M6 (LLM judge), M7-M9 (reconciliation) are explicitly out of scope for the rewrite; if Skosmos display is needed for evaluation it lands as a passive viewer, not an enrichment surface.

**Driver**: the conversion side (MARCXML ↔ BIBFRAME ↔ BFFI in both directions) is the foundation everything else builds on. Recent diagnostics — the 20 k-record run killed on b10007428's 100 × MARC 730 → 200 `bf:Hub` cross-product blow-up, the p-56 hard-cut transition committing the codebase to zero `bf:*` in the BFFI emit, the marcKey-bypass audit (P-49) exposing that the round-trip is silently smuggling cataloguer-typed MARC strings through the graph to mask structural shortfalls — all point at structural decisions in the conversion layer that are easier to bake into a fresh implementation than to retrofit. The existing pipeline carries six months of accreted reconciliation logic on top of a conversion layer that hasn't been corpus-scale-validated. A conversion-first rewrite gets the foundation right before re-investing in the downstream stages.

## In / out of scope

### In scope (rewrite branch)

Four conversion pillars + the eval harness:

1. **MARCXML export** — input from the Helmet Sierra dump (`/Users/mikkovihonen/Workspace/helmet-sierra-data-tools/output/marcxml/` per existing memory). Per-record file layout matches the existing corpus.
2. **MARC → BIBFRAME** — the LoC marc2bibframe2 XSLT (`third_party/marc2bibframe2/` submodule) wrapped by a thin driver. Behavior unchanged from main.
3. **BIBFRAME → BFFI** — SPARQL CONSTRUCT (or equivalent RDF processing) reading the marc2bibframe output and emitting BFFI-only canonical Turtle. Bakes in p-56 from day 1: zero `bf:*` in the emit, every routing per `docs/bf_to_bffi_mapping.md`.
4. **BFFI → MARC** — the reverse direction. Reads the BFFI canonical graph (BFFI predicates only — does NOT consult the BIBFRAME intermediate and MUST NOT read `bffi-prov:` pipeline-internal provenance for content decisions, per the bffi_limitations.md cardinal rule) and reconstructs MARCXML. Used for round-trip verification, the diff residue registry, and downstream MARC consumers that may want to re-derive MARC from BFFI without reading the original Sierra dump.

Plus **evaluation harness** — round-trip diff (`MARCXML → BFFI → MARCXML` end-to-end via pillars 2-4), cataloguer-review HTML, mapping-discipline tests (closed-namespace regression, marcKey-bypass counts).

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

- CLI / orchestrator — simpler shape with the five stages (export / marc2bibframe / bibframe2bffi / bffi2marc / roundtrip_eval) instead of main's M1-M10.
- Forward-conversion SPARQL (`bf_to_bffi_*.rq` or equivalent) — start from the main-branch versions as reference but emit `bffi:*` only (no `bf:*` legacy); discriminator routings from p-56 Phase 4 baked in.
- Reverse-conversion converter (BFFI → MARC) — read-side reads BFFI predicates only (no `bf:*` typing as a routing key, no `bffi-prov:` provenance as a content source). The marcKey-bypass audit from P-49 informs which subfield reconstructions are genuine vs marcKey-smuggled; the rewrite aims to ship the converter with the bypass count strictly lower than main's baseline.
- Test fixtures — reuse the gold MARCXML inputs from main; rewrite the assertion files to expect BFFI-only emit (both directions) and the new diff distribution.

## Branch policy

- The `rewrite` branch is the working line for everything in scope above.
- `main` stays as the legacy reference; commits to main are limited to: (a) documents that pertain to both branches (this proposal, mapping doc updates, lkd.rdf bumps), (b) emergency fixes if anyone runs the legacy pipeline.
- Direct-to-branch commits, per the existing direct-to-main norm. No feature branches off `rewrite` unless an experiment genuinely needs isolation.
- When the rewrite reaches corpus-scale conversion + eval parity, decide: merge `rewrite` → `main` and archive the legacy pipeline, or keep both lines.

## Phasing (broad — detailed plans land as follow-ons)

The rewrite is large enough that committing to specific phase boundaries before the first prototype runs is premature. The rough sequence:

1. **Scaffold the rewrite branch**: minimal repo layout (CLI, sparql/, third_party/ submodule reference, tests/, observability event-sidecar from day 1). Lift the carry-over assets listed above.
2. **MARC → BIBFRAME**: wrap marc2bibframe2 with the thin driver. Run on the curated dev sample (≈20 k records: a 20 k slice of the Helmet corpus unioned with the identified problem records from main — e.g. b10007428's 100 × MARC 730 cross-product) end-to-end.
3. **BIBFRAME → BFFI v0**: emit BFFI-only canonical Turtle. p-56 Phase 1 (clean rename) baked in. No discriminator routings yet — emit will fall short on Hub / Identifier-scheme / Title-variant / Series-link until step 6.
4. **BFFI → MARC v0**: reverse converter reading BFFI predicates only. Round-trip the dev sample end-to-end (MARC → BFFI → MARC).
5. **Eval harness v0**: round-trip diff + cataloguer-review HTML wrapping pillars 2-4. Establish corpus-scale baseline against main.
6. **p-56 Phase 4 routings**: Hub, Identifier-scheme, Title-variant, Series-link, Audio. The reverse converter (pillar 4) updates to read the new discriminator predicates instead of `bf:*` typing keys. Eval harness signals correctness.
7. **p-56 Phase 5 music interim**: `bffi:readMarc382` + `bffi:musicKey` literal collapse against BFFI 1.0.0 in the forward direction; reverse converter reads the literals back to MARC 382 / 384 verbatim.
8. **Full corpus run**: 800 k records, end-to-end MARC → BFFI → MARC + eval. Diagnose corpus-scale failure modes (cross-product blow-ups, memory ceilings, throughput) in both directions. The known-problem records from main (b10007428's 100 × MARC 730 cross-product + any other high-cardinality cases) are already in the 800 k corpus — the full-corpus run is the catch.

Each numbered step warrants its own plan once we have signal from the previous one.

## Open questions

1. **Submodule sharing or rewrite-branch copy?** The `third_party/marc2bibframe2/` submodule is identical on main and rewrite. Sharing via git submodule reference (the rewrite branch inherits main's pointer) is the obvious answer; flag if there's reason to vendor a separate snapshot.
2. **Skosmos as eval surface?** The round-trip diff + cataloguer-review HTML covers conversion correctness mechanically. Skosmos display adds visual inspection but requires running the Docker stack. Default to: keep the cataloguer-review HTML as the primary eval surface; add Skosmos only if it shows value during eval-harness work.
3. **Dev sample composition.** Locked in: ≈20 k records (a 20 k slice of the Helmet corpus) unioned with the identified problem records main has surfaced (e.g. b10007428's 100 × MARC 730 cross-product, plus any others discovered during scaffold + step 5 baseline). Same sample drives steps 2-7; step 8 (full corpus) is the canonical scaling check. The 13-bib curated sample from main is retired in favour of this larger one because 13 records don't surface enough variety to catch real corpus issues.
4. **Merge or replace?** When rewrite reaches parity on conversion + eval — does it merge into main (preserving the reconciliation stages there), or does it become the new main (with the reconciliation stages reimplemented or imported back later)? Defer until rewrite reaches that point.

## Verification

A successful rewrite is gauged on:

- **Closed-namespace discipline (forward direction)**: zero `bf:*` URIs in any canonical BFFI Turtle emitted by the rewrite branch. Enforced by the test extension in p-56's verification section.
- **bffi-prov discipline (reverse direction)**: the BFFI → MARC converter reads only the `bffi:` (+ `skos:` / `dct:` / `rdf:` / `rdfs:` / `owl:`) namespaces for content reconstruction. Static-source test fails the build if the reverse converter imports or queries any `bffi-prov:` predicate as a content source. Pipeline-internal provenance is fair game for UI / pairing machinery (e.g. lineage tokens on diff rows), never for emit content.
- **Round-trip parity**: round-trip diff (`MARCXML → BFFI → MARCXML`) shows the same `identical` / `changed` / `lost` / `tag-changed` / `marckey-bypass` distribution as a comparable run on main, **with the marckey-bypass count strictly lower** (Phase 4 routings read BFFI-side predicates, eliminating the bypass on Hub / VariantTitle / Identifier rows).
- **Corpus scale**: 800 k-record full-corpus conversion (both directions) completes in bounded wall-clock and memory. The b10007428 cross-product class of failure does not appear (verified by including b10007428 + similar high-cardinality records in the eval set).

## Rollback

The rewrite lives on its own branch. If it doesn't work out, the rewrite branch is deleted; main is unaffected. No data migration on either side because canonical Turtle is rebuilt from MARCXML each run on both branches.

## Progress

- ✅ **Step 1 — Scaffold** (commit `b242261`). Minimal repo layout in place: five-stage typer CLI with `NotImplementedError` stubs, observability sidecar emitter, closed-namespace machinery (`provenance/`), URI minting (`uris.py`), validation boundaries 1-3, BFFI ontology + LoC bridges under `vocab/`, marc2bibframe2 submodule, observability stack config (Caddyfile + grafana + prometheus.yml + SHACL shapes). 23 src .py files; 93 unit tests green; mypy --strict clean.
- ✅ **Step 2 — MARC → BIBFRAME wrapper** (commit `d6dcab3`). `stages/marc_to_bibframe/` shipped: `xslt.py` (subprocess shim around `xsltproc` with `XsltPaths` / `XsltResult` / `XsltprocError`), `runner.py` (`ConversionOptions` / `ConversionSummary` + `convert_one` + `convert_corpus`; preprocess+convert two-pass; observability events `start` / `progress` / `failed` / `end`). CLI: `bffi-pipeline marc-to-bibframe --input-dir … --output-dir …`. 9 new tests using the vendored `marc.xml` fixture; 102 total tests green.
- ✅ **Step 3 — BIBFRAME → BFFI v0** (commit `a7e9ab7`). `stages/bibframe_to_bffi/` shipped: `mappings.py` (rdflib parse of `vocab/lkd.rdf`, extracts every `owl:equivalentClass` / `owl:equivalentProperty` between `bffi:*` and `bf:*` — verified: 143 class equivalences, 136 predicate equivalences; lexicographically-first wins on ambiguity); `runner.py` (`rename_graph` term-by-term substitution; `convert_one` / `convert_corpus` with sidecar events; per-record `closed_namespace_residue` counter surfaces terms not yet covered, e.g. `bf:Isbn`, `bf:Hub` — these land in step 6). Output uses `bind_canonical_prefixes` so Turtle serialisation is deterministic across records. CLI: `bffi-pipeline bibframe-to-bffi --input-dir … --output-dir …`. 14 new tests; 116 total green.
- ✅ **Step 4 — BFFI → MARC v0** (commit pending). `stages/bffi_to_marc/runner.py` shipped: `_extract_bib_id_from_local` (preferred — walks `bffi:identifiedBy [ a bffi:Local ; rdf:value ?id ]`) + URI-fragment fallback handling both `http://…/<id>#Instance` and URN-style `http://urn.fi/URN:NBN:fi:bib:<id>#Instance`; `_extract_main_title` (walks `bffi:title / bffi:mainTitle`); `emit_marcxml` builds an lxml MARCXML record with placeholder leader + `controlfield 001` + `datafield 245 $a`; `convert_corpus` emits the usual sidecar events plus a `no_manifestation` counter for malformed inputs. v0 covers the minimum-viable MARC; subsequent field families (contributors, identifier schemes, subjects, provision activity, notes) land one at a time so the diff harness gives a clean per-family verification signal. CLI: `bffi-pipeline bffi-to-marc --input-dir … --output-dir …`. Plus the cardinal-rule AST scan (`test_bffi_prov_discipline.py`): fails the build if any executable code in the stage references `BFFI_PROV` / `bffi-prov:`, while still letting the module docstring describe the rule (per p-57's bffi-prov-discipline verification criterion). 11 new tests; 127 total green. End-to-end MARC → BIBFRAME → BFFI → MARC round-trip verified on the vendored fixture (`001` + `245 $a` survive intact).
- ⬜ **Step 5 — Eval harness v0.** Round-trip diff comparing source MARCXML against the reconstructed MARCXML; per-record diff classification (`identical` / `changed` / `lost` / `tag-changed` / `marckey-bypass`); cataloguer-review HTML; mapping-discipline tests. Establishes corpus-scale baseline against main.
- ⬜ Steps 6-8 to follow.

## Suggested next step

Run the full forward + reverse chain (`bffi-pipeline marc-to-bibframe` → `bibframe-to-bffi` → `bffi-to-marc`) against the curated dev sample (≈20 k + problem records). Observability dashboard surfaces throughput, failure rate, and per-stage outcome counters. Then start step 5 (eval harness v0) so the round-trip diff has a concrete shape to compare against — that's the signal source 4 (BFFI → MARC) needs to evolve from v0 minimum to per-MARC-family field coverage.

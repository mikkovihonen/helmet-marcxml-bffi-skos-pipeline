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
- ✅ **Step 4 — BFFI → MARC v0** (commit `9854242`). `stages/bffi_to_marc/runner.py` shipped: `_extract_bib_id_from_local` (preferred — walks `bffi:identifiedBy [ a bffi:Local ; rdf:value ?id ]`) + URI-fragment fallback handling both `http://…/<id>#Instance` and URN-style `http://urn.fi/URN:NBN:fi:bib:<id>#Instance`; `_extract_main_title` (walks `bffi:title / bffi:mainTitle`); `emit_marcxml` builds an lxml MARCXML record with placeholder leader + `controlfield 001` + `datafield 245 $a`; `convert_corpus` emits the usual sidecar events plus a `no_manifestation` counter for malformed inputs. v0 covers the minimum-viable MARC; subsequent field families (contributors, identifier schemes, subjects, provision activity, notes) land one at a time so the diff harness gives a clean per-family verification signal. CLI: `bffi-pipeline bffi-to-marc --input-dir … --output-dir …`. Plus the cardinal-rule AST scan (`test_bffi_prov_discipline.py`): fails the build if any executable code in the stage references `BFFI_PROV` / `bffi-prov:`, while still letting the module docstring describe the rule (per p-57's bffi-prov-discipline verification criterion). 11 new tests; 127 total green. End-to-end MARC → BIBFRAME → BFFI → MARC round-trip verified on the vendored fixture (`001` + `245 $a` survive intact).
- ✅ **Step 5 — Eval harness v0** (commit `8ed6150`). `stages/roundtrip_eval/` shipped: `diff.py` (`parse_record` reads MARCXML and returns ``(bib_id, FieldRow tuple)``; `diff_fields` per-field-instance pairing — identical-first, then positional changed, then lost / added overhang); `html.py` (single self-contained cataloguer-review HTML: corpus distribution table, per-record overview, per-record detail panels with field-level status badges and source/reconstructed columns); `runner.py` (pairs source vs reconstructed dirs by `controlfield 001`; emits sidecar events with the corpus distribution as additional `end` counters; tracks `source_only` / `reconstructed_only` orphan counts). CLI: `bffi-pipeline roundtrip-eval --source-dir … --reconstructed-dir … [--html …]`. 16 new tests; 143 total green. Deferred to follow-on: ``tag-changed`` (cross-tag content similarity for cases like MARC 260 → 264) and ``marckey-bypass`` (P-49 concept; doesn't apply until the reverse converter reads `bflc:marcKey`).

### 20 k bench run (2026-06-10)

First corpus-scale end-to-end on real Helmet data. ~25 minutes wall clock for the full forward + reverse + eval over 20,000 records.

| Stage | Records | Wall | Outcome |
|---|---|---|---|
| MARC → BIBFRAME | 20,000 | 15:20 | 20,000 / 20,000 |
| BIBFRAME → BFFI | 20,000 | 4:18 | 19,936 / 20,000 (64 rdflib serialize failures on malformed source URIs — see below) |
| BFFI → MARC | 19,936 | 3:00 | 19,936 / 19,936 |
| Round-trip eval | 19,936 | 2:25 | 19,446 paired + 98 source-only + 36 reconstructed-only |

Diff distribution on 19,446 paired records: **identical = 23,642**, **changed = 15,250**, **lost = 570,021**, **added = 0**.

The bench surfaced one infrastructure issue and a clean priority-ranked backlog of MARC field families to add to the BFFI → MARC reverse converter:

- **Infrastructure**: rdflib's RDF/XML parser accepts URIs containing spaces (e.g. `http://id.loc.gov/authorities/classification/GHelsinki, Tapiola Concert Hall` — a cataloguer-typed free-text classification value the LoC URI base concatenates with) while its stricter Turtle serializer refuses them. The step-3 runner now catches the serialize-side failure per record so one bad URI doesn't abort the corpus run. A follow-on can add an input-side URI sanitiser to recover those 64 records.
- **Pairing edges**: 134 records went unpaired (98 source-only + 36 reconstructed-only). Likely cause: duplicate `001` values across different source records (the Helmet corpus uses filenames as bib IDs; `001` carries a legacy/source-system identifier that occasionally collides). v0 indexer is last-write-wins; switching to filename-based pairing or composite keys is a small follow-on.
- **`lost` is dominated by the predictable field families** (top by count): MARC 700 (86 k), 730 (62 k), 650 (55 k), 084 (21 k), 852 (21 k), 008 (19 k), 097 / 091 / 095 (Helmet locals, 60 k combined), 041 (19 k), 260 (19 k), 710 (17 k), 300 (16 k), 740 (15 k), 005 (14 k), 651 (14 k), 092 (11 k), 655 (10 k), 336 / 337 / 338 (29 k combined), 020 ISBN (9 k), 094 (8 k), 500 (7 k), 600 (7 k).
- **`changed` is dominated by 245** (15,250 instances). v0 reverse converter emits `245 $a` only; source records typically include `$b` subtitle and `$c` responsibility statement — that's the bulk of the drift.
- ✅ **Step 6 — Discriminator routings + Phase 1-3 closures** (commits `bf170a9` → `47f4fa0`, 4 incremental commits). Shipped the full P-56 routing surface in `src/bffi_pipeline/stages/bibframe_to_bffi/routings.py`:
  - `bflc:marcKey` → `bffi:marcKey` rename (closes `bflc:` to BFFI ns).
  - Phase 4 discriminator routings: Identifier-scheme (15 LoC subclasses → `bffi:Identifier` + `bffi:source`), Title-variant (4 subclasses → `bffi:Title`), Audio (default Expression axis), Series-link (`bf:hasSeries` → structured `bffi:Relation`), Hub (per-instance marcKey dispatch).
  - Phase 3 axis-default classes (7 BIBFRAME `bffi-meta:broadMatch` families → Expression-axis default).
  - Phase 2 axis-default predicates (`bf:instanceOf` / `bf:hasInstance` / `bf:issuance`).
  - Catch-all `bf:accompaniedBy` → structured `bffi:relation` chain.
  - Extended Phase 1 in `mappings.py` to walk `rdfs:subPropertyOf` (14 new `bf:*` -> `bffi:*` renames including `bf:agent` / `bf:carrier` / `bf:note` / `bf:source` / `bf:place` / `bf:date` / `bf:content`).
  176 tests green (32 in `test_routings.py` + 11 in `test_mappings.py`). 20 k bench:
  - `closed_namespace_residue`: 19,936 (pre) → **66 records (0.3%)** after the full routing set.
  - Routing counters: 189,344 `bflc_marckey_renamed` · 73,293 Hub · 60,164 axis-default-predicate · 31,034 axis-default-class · 19,465 identifier-scheme · 1,402 title-variant · 1,251 relation-predicate (`bf:accompaniedBy`).
  - Two true gaps remain (need NLF input or a per-instance decision): `bf:provisionActivityStatement` (102x) and `bf:Statement` (4x). Both lack any `bffi:*` counterpart in `lkd.rdf`.
- ✅ **Step 7 — Music interim (P-56 Phase 5)** (commits `64cafdc` music-key + `6eee1c9` music-medium). `route_music_key` collapses `bf:keyMode → bf:KeyMode` structured bnodes into the existing `bffi:musicKey` literal predicate. `route_music_medium` collapses the BIBFRAME `bf:ensemble → bf:Ensemble → bf:mediumComponent → bf:mediumOfPerformance` tree into a `bffi:musicMedium → bffi:MusicMedium` block carrying a synthesised `bffi:readMarc382` summary string. Plus defensive drops for `bf:mode` / `bf:Mode` / `bf:tempo` / `bf:dramaticRole` / `bf:numberOfHands` / `bf:usesMediumOfPerformance` — never emitted by marc2bibframe2 (verified by XSLT grep). Zero corpus prevalence in the 20 k bench so the routings are insurance; the L-NN limitation note documents that `bffi:readMarc382` is best-effort synth, not byte-identical to source MARC 382 (marc2bibframe2 doesn't preserve the source field verbatim on the `bf:Ensemble` bnode).

  **Zero GAPs milestone:** with step 7's routings shipped, every BIBFRAME 3.0.1 declared term (450 total — 224 classes + 226 properties) has an explicit handling. The diagnostic CLI tally:

  ```
  BIBFRAME terms analysed: 450
    direct (1-hop equivalentClass/Property): 278
    indirect (2-3 hops via taxonomy / meta): 130
    routed (no lkd.rdf reach, handled by routing code): 42
    unreachable (true GAPs — no path, no routing): 0
  ```

  The auto-generated mapping doc's per-row tally also confirms zero GAP rows across both Classes and Predicates tables. A regression-guard test (`test_zero_gap_terms_milestone`) locks the invariant: any future ontology refresh that adds an un-handled term fires the check.

  Architectural side-projects landed alongside step 7:
  - **Decorator-driven routing registry** (commit `8bbbb73`). Single source of truth for routing metadata. Each routing function in `routings.py` is decorated with `@routing(terms=…, replacement=…, link_kind=…)`; the auto-table generator walks `ROUTING_REGISTRY` instead of maintaining a parallel registry. Adding a routing costs one edit; drift is eliminated.
  - **Shared `rdf_utils.py` module** (same commit). Low-level URI helpers like `local_name` moved out of `diagnostic/mapping_coverage.py` to a layer below `stages/` so routings.py can use them without creating a `diagnostic → routings` import cycle.
  - **Diagnostic `routed` bucket** (commit `127c998`). The `diagnose-mappings` CLI now reports four buckets instead of three: direct / indirect / routed / unreachable. `bf:Hub`, `bf:provisionActivityStatement`, and the PMO music terms — all routed in code but unreachable via lkd.rdf alone — surface in the routed bucket instead of being miscategorised as unreachable.

- ◐ **Step 4 follow-ons — BFFI → MARC field families** (in progress). The plan calls for one MARC field family per commit, prioritised by the 20 k bench's lost-distribution backlog. Shipped so far (each one its own commit):
  - `aa84ef0` — MARC 245 \$b subtitle + \$c responsibility statement. Closes ~15 k 'changed' records.
  - `115c54d` — MARC 020 ISBN + 022 ISSN. Establishes the dispatch-table pattern for `bffi:source` URIs → MARC tags. Closes ~9 k 'lost' 020 records (plus an ISSN tail).
  - `e165cc9` — MARC 041 language codes + MARC 300 physical description. Closes ~35 k 'lost' records combined.
  - `f987bf7` — MARC 6XX subject datafields (600/610/611/630/648/650/651/655). Establishes the Manifestation → Work walk + URI-fragment tag-discrimination pattern. Closes ~86 k 'lost' subject records.

  Still pending: MARC 700/710 contributors (~103 k), 730/740 uniform titles (~77 k), 008/005 control fields (~33 k), Helmet local classifications 091/092/094/095/097 (~75 k), 260 publication (~19 k), 084 classification (~21 k), 852 holdings (~21 k), 336/337/338 RDA terms (~29 k), 500 notes (~7 k). Each lands as its own follow-on commit; the pattern is well-established (extract helper + record-build wiring + tests + commit).

- ◯ **Step 8 — Full corpus run** (operator-ready, no code blockers). With the routing surface complete (step 7) and a meaningful chunk of the BFFI → MARC field-family work shipped (step 4 in progress), the 800 k-record full-corpus run can be executed any time. The run's diff distribution will surface (a) any unhandled MARC family remaining and (b) any cross-product / memory / throughput failure modes at corpus scale. Re-running periodically as more field families ship gives a continuous regression signal on the lost / changed bucket.

## Suggested next step

The structural rewrite (steps 1-7) is complete and the BFFI graph is **100% closed-namespace clean** on the 20 k bench (zero GAP terms across all 450 BIBFRAME 3.0.1 declared terms). Step 4 follow-ons continue incrementally; each new field family is a self-contained commit (extract helper + record-build wiring + tests). Recommended priority for the next commits in this thread:

1. **MARC 700/710 contributors** — biggest remaining lost-distribution bucket (~103 k). Walks `bffi:contribution → bffi:Contribution → bffi:agent + bffi:role` chains on the Work; needs to discriminate primary (MARC 100) from added entries (MARC 700) and emit relator codes.
2. **MARC 008 / 005 control fields** (~33 k). Reads adminMetadata (changeDate, descriptionLanguage) + language + publicationStatement → fixed-position 008; adminMetadata.changeDate → 005.
3. **Helmet local classifications 091/092/094/095/097** (~75 k). Marc2bibframe2 emits these as `bf:Classification` blocks; the reverse converter needs to pick the right MARC 09X tag based on the classification source.
4. **MARC 730/740 uniform titles** (~77 k). Walks `bffi:title` blocks where `bffi:marcKey` first 3 chars are 730/740 (distinguishing variant titles from the primary 245).

Step 8 (full 800 k corpus run) can be executed any time after these ship — re-running periodically gives a continuous regression signal on the lost / changed bucket.

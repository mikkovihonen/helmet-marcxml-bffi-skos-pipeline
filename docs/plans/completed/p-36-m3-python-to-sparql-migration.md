# P-36 — Fix M3 SPARQL CONSTRUCT routing gaps + migrate Python post-process helpers into SPARQL

**Status**: completed 2026-05-15. All three phases shipped (C first per the priority note, then B, then A). The rdflib nested-OPTIONAL bug (R1) did NOT bite Phase A — the two-pattern inner OPTIONAL (`?c bf:role ?otherRole` + nested `OPTIONAL { ?otherRole rdfs:label ?otherRoleLabel }`) propagated roles correctly across all three bug-check fixture cases (URI form, blank-node-with-label form, three-roles-one-agent form). **Originally drafted straight to backlog per operator request** (no separate proposal stage — the trade-off is captured below). **Scope expanded 2026-05-14** with Phase C after the second SPARQL routing bug was diagnosed against the helmet-5k-clean-full bench (run `02924cb38191`): 21 925 subject/genreForm targets were silently dropped by M9 because their `rdfs:label` triples never made it through M3's CONSTRUCT — same shape as the agent-label bug fixed at `d040a90`.

**Plan-base commit**: `d040a90` (the predecessor commit that proved one rdflib nested-OPTIONAL pattern — `?primaryAgent rdfs:label ?primaryAgentLabel` — works in CONSTRUCT after all). To gauge drift before executing, run
`git diff d040a90..HEAD -- sparql/bf_to_bffi_work.rq sparql/bf_to_bffi_expression.rq src/bffi_pipeline/stages/bf_to_bffi.py tests/unit/test_bf_to_bffi.py tests/integration/test_workkey.py`.
**Phase commits**:

- Phase A (non-primary `bf:role` routing): `0fc562a`. **R1 risk did not materialise.** Extended `sparql/bf_to_bffi_expression.rq`: CONSTRUCT gains `?otherContrib bf:role ?otherRole` plus `?otherRole a bf:Role ; rdfs:label ?otherRoleLabel`; the non-primary contribution OPTIONAL gains an inner `OPTIONAL { ?c bf:role ?otherRole . OPTIONAL { ?otherRole rdfs:label ?otherRoleLabel } }`. The two-pattern inner OPTIONAL (role triple + nested label OPTIONAL) is the R1 risk shape the plan flagged; bug-check probes confirmed rdflib's nested-OPTIONAL behaviour is correct for this case. Deleted `_propagate_non_primary_roles` + `_index_source_roles_by_agent` + `_emit_role_on_contribution` (~117 lines combined) plus their call site in `post_process`. Renamed three existing tests `test_post_process_propagates_* → test_construct_routes_*` and dropped their `post_process(bffi, source)` calls. Added `tests/integration/test_workkey.py::test_translator_e_role_round_trips_through_m2_m3` — pins MARC 700 `$e "kääntäjä."` end-to-end through M2 → M3 to a `bffi:Contribution` carrying `bf:role [a bf:Role ; rdfs:label "kääntäjä."]`. One observed semantic diff vs the deleted Python helper: URI-form roles (`relators/trl` etc.) now also carry an `<relators/trl> a bf:Role` typing triple in the output. Harmless — LoC's relator URIs ARE `bf:Role` instances; the typing is semantically correct and adds at most one triple per distinct relator URI per record.
- Phase B (Helmet identifier denormalisation): `79b0c8a`. Routed via both `sparql/bf_to_bffi_work.rq` and `sparql/bf_to_bffi_expression.rq` — added `dct:` PREFIX, CONSTRUCT clause `?{work,expr}URI dct:identifier ?helmetBibIdLiteral`, and `BIND(STR(?identValue) AS ?helmetBibIdLiteral)` inside the existing Helmet identifier OPTIONAL. Deleted `_emit_helmet_identifiers` (~24 lines) and its call in `post_process`'s helper sequence. The `DCTERMS` import stays — `post_process` still uses `bffi_graph.bind("dct", DCTERMS)` to bind the prefix on the output graph. Two unit tests renamed `test_post_process_* → test_construct_*` and updated to assert against `construct_bffi(source)` alone (no `post_process()` call needed for the assertion).
- Phase C (subject + genreForm `rdfs:label` routing): `b97ad6f`. Shipped with the **R7 fallback** for the CONSTRUCT clause (`?bfSubject rdfs:label ?subjectLabel` instead of `?subject rdfs:label ?subjectLabel`). The plan's primary approach would have misattributed labels in the madsrdf-tagged case: the source raw URI (e.g. `<...#Place651-54>`) carries the cataloguer-supplied `rdfs:label`, so an inner `OPTIONAL { ?bfSubject rdfs:label ?subjectLabel }` binds `?subjectLabel`; with CONSTRUCT on `?subject` (the COALESCE result = authority URI), the label would land on the authority URI in our graph (clashes with Finto's authoritative label). R7's fix routes the label onto `?bfSubject` so authority-URI subjects stay label-free; the local raw URI carries an orphan label triple (harmless — nothing else in BFFI output references it). Three unit tests pin the behaviour: `test_construct_routes_subject_label_for_local_authority`, `test_construct_does_not_route_subject_label_for_authority_uri_subjects`, `test_construct_routes_genreform_label`.

  **Phase C bench evidence** (run `d33140fce19e436aa9e9f82e8ab599f6`, 500-record bench at pipeline SHA `9f52081d`, completed 2026-05-15T05:09Z):

  | Surface | Distinct targets | With `rdfs:label` | Coverage |
  |---|---:|---:|---:|
  | `bffi:subject` | 2 892 | 2 294 | **79.3 %** |
  | `bffi:genreForm` | 493 | 431 | **87.4 %** |

  Both surfaces clear the DoD bar (subjects at 79.3 % is the COALESCE-to-authority residual where R7 deliberately suppresses the label so Skosmos resolves from Finto — operating as designed). M9 `end` event counters from the same run:

  | Counter | Pre-Phase-C (5k bench `02924cb38191`, creators-only pool) | This run (500-record, mixed pool) | Phase C signal |
  |---|---:|---:|---|
  | `total` | 4 183 | 1 983 | Subjects + genre candidates entered the pool (creators-only at this slice would be ~400; the extra ~1 600 is the previously-dropped 21 925-at-5k surface). |
  | `local` | 0 | **1 047** | Tier-0 YSO/KAUNO local resolver lit up — ~53 % of the candidates bound pre-LLM, exactly the cataloguer-hour saving R6 predicted. |
  | `no_candidate` | (dominant outcome) | 469 | Phase C labels reach the candidate generator; the residual `no_candidate` is the genuine miss surface, not the pre-fix "no label, never asked" hole. |
  | `lexical` | — | 95 | |
  | `llm_pick` | — | 153 | |
  | `fallback` | — | 94 | |
  | `fictional` | — | 125 | |
  | `watchdog_aborted` | — | 0 | |

  Full chain green: `m2 → m3 → m5 → m6 → m8 → m9 → skosify → load → export` all completed. M3 converted 495 / 500 records (the `failed_shape: 106` count is pre-existing SHACL gating, unrelated to Phase C).

**Owner**: operator (Mikko) + claude solo-then-commit. All three phases are mechanically verifiable — unit tests pin the CONSTRUCT output shape; no paired session needed.
**Estimated wall-time**: ~4 h end-to-end. Phase A: ~2 h (the rdflib-bug fallback path doubles the cost). Phase B: ~1 h. Phase C: ~1 h (the agent-label precedent at `d040a90` derisks the SPARQL change).

**Phase priority**: **Phase C ships first** when execution starts. It's the highest-impact change of the three — it unblocks subject reconciliation for ~22 k entities the bench just measured as silently dropped, where Phases A and B are code-shrink wins worth ~120 deletable Python lines combined. The phase-letter ordering is preserved for plan-history continuity (Phase A/B were drafted first); execution priority differs.

## Motivation

Three concerns sit in the same neighbourhood — M3's SPARQL CONSTRUCT (`sparql/bf_to_bffi_*.rq`) and `src/bffi_pipeline/stages/bf_to_bffi.py::post_process` together decide which triples land in the per-record BFFI output:

1. **`_propagate_non_primary_roles`** (`bf_to_bffi.py:539-592`, ~54 lines + two helpers `_index_source_roles_by_agent` and `_emit_role_on_contribution`, ~40 more lines). Copies `bf:role` (URI form for MARC `$4` + blank-node-with-`rdfs:label` form for `$e` free-text) from source `bf:Contribution` onto the minted `bffi:Contribution`. The header comment at `bf_to_bffi_expression.rq:40-47` explicitly calls this out as an rdflib SPARQL CONSTRUCT bug workaround.
2. **`_emit_helmet_identifiers`** (`bf_to_bffi.py:638-661`, ~24 lines). Denormalises the Helmet bib_id as a flat `dct:identifier` literal on Work + Expression so Skosmos can render it without dereferencing the structured `bf:Local` blank node.
3. **Subject + genreForm labels never make it through M3's CONSTRUCT**. The 2026-05-14 helmet-5k-clean-full bench (`02924cb38191`) measured 18 928 distinct `bffi:subject` targets on canonical.ttl with **0** carrying `rdfs:label`, and 2 997 `bffi:genreForm` targets, also **0** with labels. `_iter_subject_requests` at `reconcile.py:1265` does `if label_lit is None: continue`, so M9 silently dropped all 21 925 reconciliation candidates. The source BIBFRAME at `runs/.../bibframe/<bib>.rdf` does carry the labels (`<bf:Topic ...><rdfs:label>viulu</rdfs:label></bf:Topic>`); `bf_to_bffi_work.rq` routes the subject *pointer* but drops the inner `rdfs:label` triple. Same bug shape as the agent label one fixed at `d040a90`.

The commit at `d040a90` proved that **at least one** nested-OPTIONAL pattern that we previously believed bit rdflib — `?primaryAgent rdfs:label ?primaryAgentLabel` inside the PrimaryContribution OPTIONAL — actually works correctly when the inner OPTIONAL wraps a single triple. That changes the cost-benefit on Phase A: it's now plausible (not certain) that `bf:role` routing follows the same pattern. Worth a focused retry with the same commit-first-then-fallback-to-Python protocol that worked on `d040a90`. **Phase C is structurally identical to `d040a90`** (single-triple inner OPTIONAL inside an existing subject OPTIONAL block) so the rdflib bug should not bite at all.

Phase B has no SPARQL bug — it's pure Python-by-convenience. The win is consistency (post-process surface shrinks; the CONSTRUCT becomes the single source of truth for "what BFFI triples M3 emits per source record"), not bug avoidance.

## Out of scope (deliberately not migrated)

These M3 helpers stay in Python — listed here so future readers don't reopen the question:

- **`_retag_pref_labels`** (`bf_to_bffi.py:470`). Combines `bf:language` candidates, MARC 041$a, parallel-title separator detection (` = ` / ` / ` RDA glue), Lingua heuristic, and an optional LLM detector cascade. The cascade is the load-bearing path and is inherently Python. The single-declared-language fast path could theoretically be SPARQL but splitting it from the cascade fragments one decision across two languages of code — net negative.
- **`_emit_extracted_contributions`** (`bf_to_bffi.py:680`). Drives the M3 245$c contributor-extraction LLM cascade. Inherently Python — not a SPARQL candidate.
- **`_sanitize_uri_whitespace`** (`bf_to_bffi.py:293`) and **`_sanitize_date_literals`** (`bf_to_bffi.py:403`). Operate on the source graph before the CONSTRUCT runs. Fixing malformed URIs and unparseable `xsd:date` literals is bytestream-level repair, not a CONSTRUCT pattern.

## Definition of done

### Phase A — Route non-primary `bf:role` via the M3 SPARQL CONSTRUCT (solo)

- [ ] `sparql/bf_to_bffi_expression.rq` extended:
  - CONSTRUCT clause adds `?otherContrib bf:role ?otherRole .` and `?otherRoleNode a bf:Role ; rdfs:label ?otherRoleLabel .` (the second only emits when the source role is a blank node with a label).
  - WHERE clause adds an inner OPTIONAL inside the existing non-primary contribution block:
    ```sparql
    OPTIONAL {
      ?c bf:role ?otherRole .
      OPTIONAL { ?otherRole rdfs:label ?otherRoleLabel }
    }
    ```
  - The comment block at `bf_to_bffi_expression.rq:40-48` is replaced with a one-line note stating that role routing now goes via the CONSTRUCT (and citing the `d040a90` precedent that nested OPTIONALs work for the single-triple-inner case).
- [ ] **Bug check (do this first, before deleting Python).** Run the same `_convert_one()` probe used on `d040a90` against at least three fixture records with known non-primary roles — one URI role (`relators/trl`), one blank-node-with-`rdfs:label` role (`"kääntäjä"`), one record with three contributions for the same agent in different roles (the "Hogwood, Christopher" pattern documented at `bf_to_bffi.py:577-581`). Assert each emits its role triple correctly and no role bleeds across agents. If the rdflib bug reappears in any case, **stop, commit the SPARQL change with a `STOP` marker, and pivot to the Python-fallback note** rather than half-deleting `_propagate_non_primary_roles`.
- [ ] On green bug check: delete `_propagate_non_primary_roles`, `_index_source_roles_by_agent`, `_emit_role_on_contribution`, and the corresponding line in `post_process`'s helper sequence. Delete the imports that become unused (`RDF.type` checks against `V.BF.PrimaryContribution`, the `_TRANSLATOR_*` constants stay — they're used by M8's mint-anchor selection).
- [ ] Existing unit tests for role propagation under `tests/unit/test_bf_to_bffi.py` are repointed to assert the CONSTRUCT output (the assertions stay the same; the test setup may shrink because the source-graph-vs-output-graph join is now SPARQL's job).
- [ ] New integration assertion in `tests/integration/test_workkey.py` (or a new test in the same module): a record with a translator $e role round-trips to a `bffi:Contribution` carrying `bf:role [a bf:Role ; rdfs:label "kääntäjä"]`. Pick an existing synthetic fixture; don't invent a new one.
- [ ] **Regression check on the 5k bench.** Re-run M3 against the curated dev sample + the 5k bench corpus; diff the per-record `bffi:Contribution` triple counts against the pre-migration run. Expect identical counts (modulo any pre-migration role-propagation bugs the SPARQL routing happens to fix — call those out individually in the commit body).
- [ ] `make lint && make test` green.

### Phase B — Route Helmet `dct:identifier` denormalisation via the M3 SPARQL CONSTRUCT (solo)

- [ ] `sparql/bf_to_bffi_work.rq` CONSTRUCT clause extended:
  - Adds `?workURI dct:identifier ?helmetBibIdLiteral .` (the value of `rdf:value` on the Helmet `bf:Local` identifier, lifted to a flat literal).
  - Adds the `dct:` PREFIX line at the top of the file.
  - The existing `OPTIONAL { ?bfWork bf:identifiedBy ?ident . ?ident bf:source <helmet> ; rdf:value ?identValue ; ... }` already binds `?identValue`; reuse it via `BIND(STR(?identValue) AS ?helmetBibIdLiteral)`.
- [ ] `sparql/bf_to_bffi_expression.rq` mirrors the same pattern for `?exprURI dct:identifier ?helmetBibIdLiteral .` (Expressions get the same denormalised bib_id).
- [ ] Delete `_emit_helmet_identifiers` and its line in `post_process`'s helper sequence. The `DCTERMS` import stays only if other helpers reference it; otherwise drop.
- [ ] Unit test in `tests/unit/test_bf_to_bffi.py` asserting that a fixture record's BFFI output contains `<work-uri> dct:identifier "b100000010" .` and the matching Expression triple. (Replaces whatever test currently pins the Python helper's output.)
- [ ] **Regression check on the 5k bench.** Re-run M3; diff the count of `dct:identifier` triples per record. Expect exact equality with the pre-migration run.
- [ ] `make lint && make test` green.

### Phase C — Route `bffi:subject` + `bffi:genreForm` `rdfs:label` via the M3 SPARQL CONSTRUCT (solo, ship first)

Diagnosed against `runs/02924cb38191/`: 18 928 `bffi:subject` targets + 2 997 `bffi:genreForm` targets on `canonical.ttl`, **zero of either** carrying `rdfs:label`. `_iter_subject_requests` at `reconcile.py:1265` silently drops every label-less target — `total: 4183` on the M9 end event was creators only; 21 925 subject/genre candidates never made it into the picker pool.

- [ ] `sparql/bf_to_bffi_work.rq` extended:
  - CONSTRUCT clause adds `?subject rdfs:label ?subjectLabel .` and `?workGenre rdfs:label ?workGenreLabel .`.
  - The existing subject OPTIONAL block (currently `?bfWork bf:subject ?bfSubject . OPTIONAL { ?bfSubject madsrdf:isIdentifiedByAuthority ?bfSubjectAuth } BIND(COALESCE(?bfSubjectAuth, ?bfSubject) AS ?subject)`) gains an inner `OPTIONAL { ?bfSubject rdfs:label ?subjectLabel }` — load-bearing detail: route the label off `?bfSubject` (the *source* subject node), not off `?subject` (the COALESCE result), because the source authority node is the one that actually carries the cataloguer-supplied label in the marc2bibframe2 output. When `?bfSubjectAuth` binds (P-15 path), `?subject` resolves to the authority URI which has no label on the local graph and the OPTIONAL fires nothing — that's correct, Skosmos resolves authority labels from the loaded Finto graphs at render time.
  - The existing genreForm OPTIONAL block (`OPTIONAL { ?bfWork bf:genreForm ?workGenre }`) gains an inner `OPTIONAL { ?workGenre rdfs:label ?workGenreLabel }`.
- [ ] **Bug check first** — same `_convert_one()` probe used on `d040a90`. Inputs:
  - A record with a local `Topic650-NN` subject + `rdfs:label "viulu"` (the `b10189452` shape from the diagnostic) — expect the BFFI output to carry `<...#Topic650-23> rdfs:label "viulu"`.
  - A record with a `madsrdf:isIdentifiedByAuthority`-tagged subject (the P-15 path) — expect `bffi:subject <auth-uri>` with NO label routed (the COALESCE picked the authority URI, which has no source-side label).
  - A record with a `bffi:genreForm` target carrying `rdfs:label` in BIBFRAME — expect the label routed.
  - If any fails: STOP, commit the SPARQL change with a `STOP` marker, pivot to a fallback (Python post-process mirroring `_propagate_non_primary_roles`'s shape) and document the rdflib bug repro pattern.
- [ ] No Python deletes — Phase C is a routing **fix**, not a migration. The post-process helpers it would have replaced never existed because the bug went undiagnosed.
- [ ] Unit tests in `tests/unit/test_bf_to_bffi.py`:
  - `test_construct_routes_subject_label_for_local_authority` — pins the `Topic650-NN` → `rdfs:label` round-trip for the dominant case.
  - `test_construct_does_not_route_subject_label_for_authority_uri_subjects` — pins the P-15 COALESCE-to-authority path; asserts no spurious label triple on a `yso/pNNNN` subject.
  - `test_construct_routes_genreform_label` — symmetrical pin for `bffi:genreForm`.
- [ ] **Regression check on the 5k bench.** Re-run M3 + M8 + M9 on `marcxml/samples/helmet/5000/marcxml`. The diagnostic counts must invert: `bffi:subject` targets with `rdfs:label` on canonical.ttl jumps from 0 to ≥ 80% of distinct targets (some legitimate label-less authority-URI subjects remain). M9 `end` event's `total` counter rises from 4 183 to ≥ 20 000. The `local` counter rises from 0 into the thousands as YSO/KAUNO matches hit the tier-0 path.
- [ ] `make lint && make test` green.

### Cross-phase

- [ ] On graduation to `in-progress/` (first phase merged), `git mv` the plan from `backlog/` to `in-progress/`.
- [ ] On final phase merge with all DOD boxes checked, `git mv` to `completed/`.
- [ ] One-paragraph snapshot in the per-phase commit body — what migrated, how many lines deleted, the bug-check probe output. No separate `docs/performance/` file (the change is internal refactor, not a behaviour shift).

## Risks

- **R1 — The rdflib nested-OPTIONAL bug reappears in the role-routing case.** The `d040a90` precedent only proves the bug doesn't bite when the inner OPTIONAL is a single triple (`?agent rdfs:label ?label`). The role case wraps **two** patterns inside the OPTIONAL — `?c bf:role ?role` AND `OPTIONAL { ?role rdfs:label ?label }`. If that re-triggers the binding bug, Phase A reverts to the existing Python path (zero net change; commit the SPARQL exploration with a `STOP` marker and a one-paragraph note explaining the bug repro pattern). The DOD's "bug check, do this first" gate makes this a cheap fast-fail, not a multi-hour dead-end.
- **R2 — Role-with-blank-node typing in CONSTRUCT.** The blank-node-with-`rdfs:label` role form requires the CONSTRUCT to emit `?otherRole a bf:Role` AND `?otherRole rdfs:label ?otherRoleLabel`. rdflib's CONSTRUCT can do this — but the blank-node identity has to be the *same* node across both triples. Specifying both clauses on `?otherRole` (not on a fresh `BNODE()` placeholder) keeps identity stable; this is the same pattern that already works for `?contrib a bffi:PrimaryContribution ; bffi:agent ?primaryAgent` after the `BIND(BNODE() AS ?contrib)` trick. The DOD's three-fixture probe covers this.
- **R3 — Cross-agent role bleed.** The Python helper at `bf_to_bffi.py:577-581` deliberately handles the "same agent contributed three times in three roles" case via a per-agent queue. The SPARQL CONSTRUCT relies on the source's 1:1 `bf:Contribution` → `bf:agent` + `bf:role` join inside the OPTIONAL — which is correct in principle but worth a regression test (covered by the third fixture in the DOD bug-check).
- **R4 — `dct:` PREFIX collision.** No existing pipeline SPARQL uses `dct:`. The Python helper imports `from rdflib.namespace import DCTERMS`. Phase B adds the prefix; nothing collides. (Mentioned only to flag for future readers — not a real risk.)
- **R5 — Phase B has no SPARQL bug, so the migration is pure refactor with regression risk.** The 5k-bench diff is the safety net. If the diff is non-zero, the pre-migration helper's behaviour deviates from naive denormalisation in some way (defensive isinstance check skipping non-`Literal` `rdf:value` cases?); investigate before merging.
- **R6 — Phase C may surface a wave of false-positive M9 reconciliations once the 21 925 dropped candidates re-enter the pipeline.** Many of those subject literals (`"viulu"`, `"Kiina"`, common Finnish terms) will bind cleanly to YSO/KAUNO via the tier-0 local resolver — that's the win — but the literals also include cataloguer typos, abbreviations, and English-source records whose subjects don't bind to anything (`no_candidate` rate already at 61% pre-fix). Expect M9 wall-time to rise materially; the no_candidate count may climb proportionally. Not a blocker; it's the surface that was hidden. The 5k bench diff is the safety net for catching catastrophic regressions (e.g., M9 wall > 4× the pre-fix baseline suggests a runaway picker dispatch).
- **R7 — Phase C's COALESCE-to-authority subjects shouldn't carry routed labels.** The CONSTRUCT extends the existing `OPTIONAL { ?bfWork bf:subject ?bfSubject ... }` block; the inner `OPTIONAL { ?bfSubject rdfs:label ?subjectLabel }` only binds when the source `?bfSubject` (pre-COALESCE) has a label. P-15-style subjects with `madsrdf:isIdentifiedByAuthority` produce `?subject = ?bfSubjectAuth` (the authority URI) but the routed label triple is `?subject rdfs:label ?subjectLabel` — referencing the COALESCE result. If `?bfSubject` (the local source node) carries `rdfs:label` AND the COALESCE picked the authority URI, the label would be misattributed to the authority URI. The probe fixture for the second bug-check case pins this; if it fires, switch the CONSTRUCT clause to `?bfSubject rdfs:label ?subjectLabel` instead, and rely on the COALESCE branch's no-source-label case to keep authority-URI subjects label-free.

## Rollback procedure

Each phase is a self-contained SPARQL+Python pair. To revert Phase A: revert the commit; the Python helpers + their call site come back automatically and the SPARQL CONSTRUCT loses its role clauses. Same for Phase B. For Phase C: revert the commit; the subject + genreForm labels stop flowing and M9 returns to its pre-fix behaviour of silently dropping 21 925 candidates (which is the bug, not a feature — so revert only if the regression check reveals a worse issue). No dashboard, no config-flag, no data-format change downstream — pipeline output is identical pre/post-migration on the regression check (except Phase C, which intentionally adds the missing label triples).

## Composition with sibling plans

- **P-35 (M3 cascade follow-ups)** — independent. P-36 touches the CONSTRUCT + the post-process, not the cascade extractor. If P-35 Phase F2 / F3 land in parallel, the two plans don't interact.
- **P-33 (M3 Manifestation + Item CONSTRUCT)** — sequenced **after** P-36. P-33 will add a third SPARQL file alongside `bf_to_bffi_work.rq` + `bf_to_bffi_expression.rq`; doing P-36's refactor first means P-33 inherits a leaner post-process surface.
- **P-15 (preserve authority URIs at M3, completed)** — already in the CONSTRUCT (`?subject` COALESCE pattern at `bf_to_bffi_work.rq:87-91`). P-36 doesn't touch that path.

# Minimum bibliographic information + synthesis contract

This doc names the **minimum-set bar** every record must meet before it is exported to Skosmos, catalogues every place the pipeline **synthesises** a value to meet that bar, and points consumers at the artefacts that document what was synthesised on any given run. It is the contract Phases B + C of P-41 consume; future plans extending the salvage taxonomy add rows to the table below without re-litigating the contract.

**Owner**: P-41 in-progress (see `docs/plans/in-progress/p-41-minimum-bibliographic-synthesis-and-export-report.md`).

## The minimum exportable set

A record reaches Skosmos only if its MARCXML carries — or has the M2 salvage layer fill in — the fields below. The bar is enforced by `bffi_pipeline.validation.marcxml.validate_minimum_content` (Boundary 1).

| MARC field | What it carries | Why it's mandatory | Salvageable today? |
|---|---|---|---|
| **245** ($a + $b) | Title proper + subtitle | A Work without a title cannot be addressed by name in Skosmos. | No (out of scope; cataloguer must fix the source). |
| **1XX or 7XX** | Primary or non-primary creator | Authorship is the BFFI ontology's primary access surface; a Work needs an Agent edge to render. | **Yes — P-41 Phase B.** |
| **008** (positions 7-14, 35-37) | Publication date + language | Date drives Expression discrimination; language drives `skos:prefLabel` tagging. | No (the cost of an 008 synthesiser exceeds the benefit at the measured 0.02 % drop rate). |
| **336 / 337 / 338** | RDA content / media / carrier type | Drives BFFI subclass routing for Works (book vs music vs map vs video). | **Yes — already shipped (P-08).** |

The bar deliberately does NOT include language (008/35-37). Language is *detected* downstream (M3's `language_detect`) when not asserted, and the absence-of-language case is handled by the `und` tag — not a drop reason.

## Synthesis-policy table

Every place the pipeline writes a value the source MARCXML did not contain. Phase A's audit catalogued each path; subsequent plans add rows.

| Field synthesised | Stage | Method | Marker on the wire | Confidence band | Owner / contract |
|---|---|---|---|---|---|
| MARC 336 / 337 / 338 | Sierra export (pre-M2) | P-08 cascade: 007 → (leader/06, 008-form) → bib material code → itype → 300$a extent | `$5 FI-HELME/synth-v<N>` subfield on each synthesised datafield | n/a (deterministic from leader + 008 + 007; near-100 % coverage) | `docs/plans/completed/p-08-richer-rda-33x-synthesis.md` |
| `skos:prefLabel` `@xx` tag | M3 (`language_detect.py:_retag_pref_labels`) | Cataloguer-declared MARC 041 + Lingua statistical detection + optional local-mlx-lm cascade for ambiguous parallel-title cases | None today — gap is documented; downstream stages don't currently distinguish detected-tag from cataloguer-tagged | High when MARC 041 is single-language (cataloguer is authoritative); medium when Lingua picks from MARC 041's declared candidates; low when LLM cascade reconciles parallel titles | M3 stage isolation — no plan owns the marker gap today; future plan should add a `bffi-prov:Synthesis` Activity carrying `syntheticField "skos:prefLabel/@xx"`. |
| Date `xsd:date` datatype | M3 (`sanitize.py:_sanitize_date_literals`) | Strip datatype from unparseable cataloguer placeholders (e.g. `"19  -  -  T00:00:00"`); the lexical value is preserved as a plain string | None | n/a (conservative repair: no value is fabricated, only the datatype assertion is dropped) | M3 stage isolation. Treated as repair not synthesis — no `bffi-prov:Synthesis` Activity. |
| Canonical Work URI (mint fallback) | M8 (`mint.py:284`) | Fallback for synthetic-fixture inputs that don't populate the canonical mint key. **Test-only path** in production. | None | n/a | Test infrastructure; not exercised in production runs. |
| `bf:contribution` / `bf:agent` (1XX/7XX salvage) | **M2 (`marcxml_repair.py` — P-41 Phase B; not yet shipped)** | Tiered: B1 = 245$c regex parse + LLM cascade fallback; B2 = publisher-as-corporate-creator (feature-flagged off); B3 = anonymous-by-convention sentinel | `$5 FI-HELME/synth-v1` on the synthesised MARC 710 datafield + `bffi-prov:Synthesis` Activity in the provenance graph | B1 regex 0.5–0.8; B1 LLM 0.7 (capped); B2 0.3; B3 0.1 | This plan, Phase B. Sentinel agent: `http://urn.fi/URN:NBN:fi:bib:agent:unknown`. |

**Reading the table**: every row that has a `bffi-prov:Synthesis` Activity in its marker column is a row Phase C's per-run TSV writes a line for. Rows that synthesise without a Synthesis Activity (today's M3 language detection + M3 date-datatype repair) are pre-existing behaviour kept for compatibility and are NOT surfaced through the P-41 TSV; future plans that elevate either to full synthesis treatment will add a Synthesis Activity at the emit point and the TSV will pick them up automatically.

## Synthesis vocabulary

P-41 Phase A added five `bffi-prov:` predicates + one `bffi:` predicate + one committed-identifier URI to the project's vocabulary. They live in `src/bffi_pipeline/provenance/vocab.py` and are documented at the source. Summary:

| Term | URI | Type | Purpose |
|---|---|---|---|
| `bffi-prov:Synthesis` | `…bffi-prov#Synthesis` | Activity class | Sibling of `bffi-prov:MarcConversion`. One Activity per (record, synthesised field) tuple. |
| `bffi-prov:syntheticField` | `…bffi-prov#syntheticField` | datatype property (string) | The BFFI field path synthesised — e.g. `"bf:contribution/bf:agent"`. |
| `bffi-prov:syntheticMethod` | `…bffi-prov#syntheticMethod` | datatype property (string) | Free-text method tag — e.g. `"creator-from-245c (regex)"`. |
| `bffi-prov:syntheticTier` | `…bffi-prov#syntheticTier` | datatype property (string) | Phase B tier ID — `"B1"`, `"B2"`, `"B3"`, or a tier string from a future plan. |
| `bffi-prov:syntheticConfidence` | `…bffi-prov#syntheticConfidence` | datatype property (xsd:decimal) | 0.0 to 1.0. Tier-specific bands. |
| `bffi:syntheticSentinel` | `…bffi:syntheticSentinel` | datatype property (xsd:boolean) | Marks synthetic-sentinel resources (Agents, Works) that downstream stages must NOT key on. The B3 sentinel agent carries this triple. |
| `http://urn.fi/URN:NBN:fi:bib:agent:unknown` | committed URI | resource | The single shared B3 sentinel agent URI. Defined at `bffi_pipeline.provenance.vocab.SENTINEL_AGENT_UNKNOWN`. |

Every Synthesis Activity carries the same predicate set: `prov:used` → the source MarcConversion Activity, `prov:generated` → the synthesised resource (Contribution blank node, Agent, etc.), and the four `bffi-prov:synthetic*` predicates above. Extending the salvage taxonomy in a future plan = adding new `syntheticTier` string values + new methods; the predicate set stays stable.

## Cataloguer-facing artefacts

Two per-run TSVs surface different cataloguer actions on synthesised records:

- `<BFFI_DATA_DIR>/cataloguer-source-review-<run_uuid>.tsv` — append-only record of every bib ID whose source MARCXML the pipeline refused or partially refused. P-41 Phase B keeps writing a `severity=warning` row here for every record that triggered the salvage layer, so the source-side cataloguer signal is preserved. (Written by `bffi_pipeline.cataloguer_review`.)
- `<BFFI_DATA_DIR>/export-synthesis-<run_uuid>.tsv` — append-only record of every field the pipeline synthesised. One row per (bib_id, field, tier) tuple. Columns: `bib_id`, `field`, `marc_source`, `synthesised_value`, `tier`, `method`, `confidence`, `activity_uri`. **This is the consumer-audit surface**: "what did the pipeline make up on this run?" (Written by `bffi_pipeline.export_synthesis`, added in P-41 Phase C; the retrospective CLI `bffi-pipeline export-synthesis-report --run <uuid>` can rebuild the TSV from `data/provenance.ttl` alone.)

The two surfaces serve different audiences. The source-review TSV tells cataloguers *which records they should fix at the source*; the export-synthesis TSV tells consumers *what the published RDF contains that wasn't in the source MARCXML*. A record that triggered salvage will appear in both: once with `severity=warning` in source-review (cataloguer signal), once with its synthesised values in export-synthesis (consumer signal).

## Consumer-side filtering

Skosmos overlay (`config/overlay/bffi-skos-overlay.ttl`) and any downstream SPARQL queries can filter synthesised data with these patterns:

```sparql
PREFIX bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:>
PREFIX bffi-prov: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#>
PREFIX prov: <http://www.w3.org/ns/prov#>

# Hide synthesised Contributions
SELECT ?work ?contribution WHERE {
  ?work bf:contribution ?contribution .
  FILTER NOT EXISTS {
    ?activity a bffi-prov:Synthesis ;
              prov:generated ?contribution .
  }
}

# Hide sentinel-agent Contributions only (B3), keep B1/B2 synthesised agents
SELECT ?work ?contribution WHERE {
  ?work bf:contribution ?contribution .
  ?contribution bf:agent ?agent .
  FILTER NOT EXISTS { ?agent bffi:syntheticSentinel true . }
}

# Filter by confidence floor
SELECT ?contribution ?confidence WHERE {
  ?activity a bffi-prov:Synthesis ;
            prov:generated ?contribution ;
            bffi-prov:syntheticConfidence ?confidence .
  FILTER (?confidence >= 0.5)  # exclude B3 sentinel (0.1) and B2 publisher (0.3)
}
```

NLF's downstream consumers receive the published RDF dataset with all three synthesis tiers active (modulo B2 which ships behind a feature flag default-off until cataloguer leader/06 sign-off — see `docs/external-dependencies.md` Ask 6). The dataset-level publication policy (whether to suppress sentinel agents at publish time, whether to expose the synthesis trail in the public RDF or strip it) is out of scope for P-41 and lives in the publication plan.

## Cross-references

- `docs/plans/in-progress/p-41-minimum-bibliographic-synthesis-and-export-report.md` — the plan that owns this spec.
- `docs/plans/completed/p-08-richer-rda-33x-synthesis.md` — sibling synthesis pattern (33X).
- `docs/plans/backlog/p-39-m9-non-primary-contribution-reconciliation.md` — downstream consumer of the `bffi:syntheticSentinel` flag; M9 walker skips agents carrying it.
- `docs/external-dependencies.md` Asks 5 + 6 — cataloguer confirmations gating B3 label wording + B2 leader/06 activation.
- `CLAUDE.md` § "Committed identifiers" — pins `http://urn.fi/URN:NBN:fi:bib:agent:unknown` as a committed identifier.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 — bffi-prov vocabulary reference doc.
- `src/bffi_pipeline/provenance/vocab.py` — canonical source for the predicates and URIs documented above.
- `src/bffi_pipeline/validation/marcxml.py` — `validate_minimum_content` enforces the bar.
- `src/bffi_pipeline/stages/m2/marcxml_repair.py` — P-41 Phase B salvage dispatch site (not yet shipped at the time of this doc's first commit).
- `src/bffi_pipeline/export_synthesis.py` — P-41 Phase C per-run TSV writer (not yet shipped).

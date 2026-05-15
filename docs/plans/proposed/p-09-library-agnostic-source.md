# P-09 — Decouple bffi_pipeline from FI-HELME so other Finnish libraries can reuse it

**Status**: proposed.
**Scope**: 2-3 days. Phase A (read bib_id from MARC 001 instead of filename) is half a day. Phase B (pull the FI-HELME URI cluster into config) is 1-2 days. Phase C (rename `helmet-map.jsonl` and the `HELMET_MAP_FILENAME` constant) is half a day if we decide to do it.
**Proposal-base commit**: `1678e1c`. The "Motivation" reasons about the state immediately after `M3/M8: pass Helmet bib_id through to dct:identifier verbatim` (which removed `src/bffi_pipeline/helmet.py`). If `main` has moved before this is acted on, re-verify with
`git diff 1678e1c..HEAD --
src/bffi_pipeline/config.py
src/bffi_pipeline/provenance/vocab.py
src/bffi_pipeline/stages/m2/runner.py
src/bffi_pipeline/stages/m3/runner.py
src/bffi_pipeline/stages/m8/runner.py
src/bffi_pipeline/stages/m9/ysa_disambiguation_report.py
src/bffi_pipeline/validation/marcxml.py`.

## Motivation

The project is pro bono and earmarked for the National Library of Finland (`CLAUDE.md` § "Operating constraints"). NLF's mission covers every Finnish library, not just Helmet, but `bffi_pipeline` currently hard-bakes FI-HELME assumptions in nine call sites across five files:

| Location | Hardcoded fact |
|---|---|
| `provenance/vocab.py:89` | `RECORDING_SOURCE_HELMET = <…bib:recording-source/helmet>` |
| `provenance/vocab.py:91` | `HELMET_SOURCE_URI = <…bib:source:helmet>` (config-overridable, but the name + default are Helmet-specific) |
| `stages/m2/runner.py:54` | `_HELMET_RECORD_NS = "http://urn.fi/URN:NBN:fi:bib:helmet/"` (no env override) |
| `stages/m2/runner.py:345` | `g.add((ident, V.BF.source, V.HELMET_SOURCE_URI))` — emits the Helmet source on every record, unconditional on MARC 003 |
| `stages/m2/runner.py:386` | `helmet_record = URIRef(f"{_HELMET_RECORD_NS}{helmet_id}")` — `bffi:sourceMetadata` target |
| `stages/m8/runner.py:404` | `f"{graph_base}ident/helmet/{bib_id}"` |
| `stages/m8/runner.py:992` | `f"{graph_base}helmet/{bib_id}"` |
| `stages/m8/runner.py:62` | `HELMET_MAP_FILENAME = "helmet-map.jsonl"` |
| `stages/m9/ysa_disambiguation_report.py:78` | `_HELMET_IDENT_PREFIX = "http://urn.fi/…/ident/helmet/"` |

The information needed to drive every one of those triples already lives in **MARC controlfield 003** (the ILS source code, `FI-HELME` in Helmet's case — Vaarakirjastot would carry `FI-VAARA`, Heili `FI-HEILI`, etc.). The export tool sets 003; marc2bibframe2 surfaces it as `bf:Agent[bf:code]` on the BIBFRAME side; the pipeline currently ignores both and writes the FI-HELME URI unconditionally.

Two separate but related facts also feed the same direction:

1. **bib_id is currently derived from the filename stem** (`validation/marcxml.py:75`, `helmet_bib_id_from_filename`). The export's filename convention is therefore part of the pipeline's contract. After this proposal, the filename should be opaque to the pipeline — everything the pipeline needs comes from MARC content. This unlocks future batched-MARCXML inputs (multiple records per file) without retooling.
2. **The legacy bare-digits filename pattern** (`^\d+\.xml$`, accepted alongside `^b\d+[\dx]\.xml$` at `validation/marcxml.py:36`) becomes dead the moment Phase A lands: the pipeline stops looking at filenames for identity, so the dual-form support is no longer needed.

## Approach

Three sequenced phases. Phase A is independent and shippable on its own; B and C are best done together but C is optional.

### Phase A — Read bib_id from MARC 001, not from filename

- New helper `bib_id_from_record(tree: etree._ElementTree) -> str` in `validation/marcxml.py` that returns the text of the single `<controlfield tag="001">` after stripping leading whitespace. Raise `marcxml-content-minimum` if 001 is missing or empty (we already check that field; just centralise the read).
- Delete `helmet_bib_id_from_filename` and `_FILENAME_PATTERN`'s legacy `\d+` branch. The remaining pattern becomes `^.+\.xml$` — i.e. just "is this a MARCXML file?" — or drop the filename check entirely and rely on the XML/XSD validations to catch non-MARCXML inputs.
- `ValidatedMarcXml.helmet_bib_id` renames to `bib_id` and is populated from the record, not the path. (Renaming the dataclass field has small mypy + test ripple — manageable.)
- M2 still writes `<output_dir>/bibframe/<bib_id>.rdf`; the change is purely "where does `bib_id` come from".
- `bffi-prov:helmetBibId` is unchanged in Phase A. The rename + pairing with `bffi-prov:sourceCode` lands in Phase B (see below) — folded together because both touch the `bffi-prov` namespace and a single vocabulary version bump covers both.

### Phase B — Pull the FI-HELME URI cluster into config keyed on MARC 003

- Add a `LibrarySource` model in `config.py`:
  ```python
  class LibrarySource(BaseModel):
      marc_003_code: str            # e.g. "FI-HELME"
      source_uri: str               # bf:source target
      record_ns: str                # bffi:sourceMetadata namespace
      recording_source_uri: str     # bffi:recordingSource target
      identifier_path: str          # ident/helmet/ → ident/<slug>/
      label_slug: str               # for filenames, e.g. "helmet" in helmet-map.jsonl
  ```
- `Settings.library_sources: dict[str, LibrarySource]` keyed by `marc_003_code`. Default ships with the one FI-HELME entry whose URIs match the existing committed identifiers verbatim — so the change is a no-op for Helmet data.
- M2 reads MARC 003 on the record, looks up the `LibrarySource`, and writes the matching URIs into `bf:source`, `bffi:sourceMetadata`, `bffi:recordingSource`. Unknown 003 → typed error `marcxml-unknown-source` (sits alongside the four existing M2 error families).
- `provenance/vocab.py`'s `HELMET_SOURCE_URI` and `RECORDING_SOURCE_HELMET` constants disappear. Callers consult the `LibrarySource` instead.
- M8's `f"{graph_base}ident/helmet/{bib_id}"` and `f"{graph_base}helmet/{bib_id}"` become `f"{graph_base}ident/{source.label_slug}/{bib_id}"` and `f"{graph_base}{source.label_slug}/{bib_id}"`.
- `ysa_disambiguation_report.py`'s `_HELMET_IDENT_PREFIX` is computed from the `LibrarySource` set, not hardcoded.
- **Rename `bffi-prov:helmetBibId` → `bffi-prov:bibId`** and add the companion `bffi-prov:sourceCode` carrying the MARC 003 value (`"FI-HELME"` for Helmet). The pair sits on the `bffi-prov:MarcConversion` Activity; together they form the globally-unique key `(sourceCode, bibId)`. In Finnish practice the `sourceCode` value matches the library's ISIL, but the predicate is named for what the literal *is* (a MARC Organization Code, per MARC 003) rather than the ISIL-equivalence it happens to have — leaves room for the rare case where MOC ≠ ISIL without making the predicate name a lie.
- The `bffi-prov` namespace is on `CLAUDE.md`'s committed-identifier list, so this is a vocabulary version bump — call it **bffi-prov v2**. Document the new predicate set under § 8 of `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` (the Activity-class enum already lives there per `CLAUDE.md`'s pointer to that section).
- One-shot migration for existing `data/provenance.ttl`: a `bffi-pipeline provenance migrate-v2` CLI walks every `bffi-prov:helmetBibId` triple, emits a `(bffi-prov:bibId, bffi-prov:sourceCode="FI-HELME")` pair on the same Activity, drops the legacy predicate, and writes atomically. No backwards-compat shim in the reader path (per `CLAUDE.md` § "Avoid backwards-compatibility hacks").

The committed-identifier promise in `CLAUDE.md` is **preserved on the URI side**: the default `LibrarySource` for FI-HELME emits exactly the four listed URIs (`work:`, `expression:`, `source:helmet`, `graph:`). The bffi-prov v2 bump is acknowledged as a vocabulary change — `CLAUDE.md` § "Committed identifiers" should be updated in the same commit that lands Phase B to record the new predicate names.

### Phase C — Rename `helmet-map.jsonl` → `source-map.jsonl` (or `bib-map.jsonl`)

Cosmetic but completes the decoupling. Existing data dirs would need the file renamed at upgrade time (or the loader can fall back to the old name for one release). Skip if Phase B feels like enough; revisit when a second library actually onboards.

## Prerequisites

- The bib_id-passthrough commit (`1678e1c`) has shipped — Phase A builds on the assumption that `rdf:value` and `dct:identifier` already pass MARC 001 through verbatim.
- Confirmation that the export tool reliably writes MARC 003 (`FI-HELME` for all 800k Helmet records). Spot-check: `grep -L 'tag="003"' /tmp/sierra-smoke/*.xml | wc -l` should be 0.
- No active plan touches `validation/marcxml.py`, `provenance/vocab.py`, or the M8 ident-URI minter (checked at `docs/plans/in-progress/` — empty besides the README).

## Risks

- **Graph diff for the existing 800k corpus is zero or near-zero**: for FI-HELME the default `LibrarySource` produces byte-identical triples to today. The risk is a typo in the default block — covered by a unit test that pins one record's M2/M3/M8 output before and after the refactor.
- **Skosmos overlay assumes a single source**: `config/overlay/bffi-skos-overlay.ttl` labels predicates ("Helmet bib ID" / "Helmet-tunniste" / "Helmet-bib-id"). If we onboard a second library, those labels stay Helmet-coloured. Out of scope for P-09; a separate i18n pass picks it up.
- **Multi-source merge semantics**: M8 currently unions identifiers across raw Works under the assumption they're all Helmet. If two libraries describe the same Work, the merger should produce one canonical Work with both `bf:identifiedBy` chains. The current code path supports this structurally — `seen_bib_ids` is per-record, not per-source — but the proposal should validate it doesn't accidentally dedupe across sources by bib_id collision (bib_id `12345` could exist in Helmet AND Vaara). Phase B adds a `(source, bib_id)` key on the dedup set.
- **`bffi:metadataLicensor` = CC0 stays hardcoded**: CC0 is the project's published-data policy (`CLAUDE.md` § "Operating constraints"), not a per-source decision. P-09 deliberately does not parameterise it. If a future contributor's metadata is CC-BY, that's a separate proposal.
- **Unknown 003 codes are a new failure mode**: today the pipeline accepts anything because it never reads 003. Phase B's `marcxml-unknown-source` typed error makes mis-configured corpora fail fast — desired behaviour, but worth documenting in the operator runbook.
- **Provenance migration is destructive**: `provenance migrate-v2` rewrites `data/provenance.ttl` in place. Mitigation: the rewrite is purely local (one predicate renamed, one new triple added per Activity) and a `git diff` on the provenance file before/after on a 100-record sample is enough to spot anomalies; the file is regenerable from a re-run of M2 if the migration goes sideways.

## Open questions

- **Predicate name: `sourceCode` vs `sourceIsil` vs `marcOrgCode`?** I've drafted `bffi-prov:sourceCode` because it describes the literal accurately without committing to a standards-body claim that may not always hold (MOC ≠ ISIL outside the Finnish-NLF-registered set). Confirm with NLF that the MARC 003 value (vs e.g. a normalised ISIL registration string) is the right canonical key for cross-library forensics — likely yes, since 003 is what's actually present in the source data, but worth a one-line confirmation before the v2 vocabulary version ships.
- **Should `bffi-prov:sourceCode` also denormalise onto each Work in the BFFI graph (for fast Skosmos/SPARQL filters), or stay provenance-only?** Lean provenance-only — the BFFI graph already encodes source via `bf:source → LibrarySource.source_uri`. Denormalising adds redundancy without new information.
- **Should the `LibrarySource` registry live in `config.py` or in a YAML file under `config/`?** YAML is more idiomatic for what is effectively a static lookup table (the four URIs and a slug per library). `config.py` keeps everything in one place but the dict-of-models is awkward in env-var-driven settings. Lean YAML.
- **What does the export tool do when MARC 003 is missing or wrong?** Today the pipeline doesn't notice. After P-09 the pipeline raises `marcxml-unknown-source`. Should the export tool back-fill 003 if a corpus lacks it? Outside `bffi_pipeline`'s scope; flag for the export-tool maintainer.
- **Counterpoint — does this actually unblock other-library reuse?** Phase A + B make the *attribution* machinery generic. They do not address ILS-specific export ergonomics — Vaarakirjastot uses Koha, not Sierra, and would need its own export tool (analogous to `marcxml-export-sierra`) to emit MARCXML with 003=`FI-VAARA`. P-09 makes the downstream pipeline ready to receive that output; the ILS-specific work is per-library, not in scope here.
- **Alternative — leave it hardcoded**: until a second library actually wants to run this pipeline, the decoupling is theoretical. The counterargument is: every additional commit to FI-HELME-specific URIs makes the eventual refactor more expensive, and Phase A pays for itself immediately by ending the filename-as-contract coupling regardless of multi-library ambition.

## Cross-references

- [`src/bffi_pipeline/config.py`](../../src/bffi_pipeline/config.py) — where `LibrarySource` lands.
- [`src/bffi_pipeline/provenance/vocab.py`](../../src/bffi_pipeline/provenance/vocab.py) — `HELMET_SOURCE_URI` and `RECORDING_SOURCE_HELMET` constants to remove.
- [`src/bffi_pipeline/validation/marcxml.py`](../../src/bffi_pipeline/validation/marcxml.py) — `helmet_bib_id_from_filename` and `_FILENAME_PATTERN` to remove or simplify.
- [`src/bffi_pipeline/stages/m2/runner.py`](../../src/bffi_pipeline/stages/m2/runner.py) — `_HELMET_RECORD_NS`, `_add_helmet_identifier`, `_admin_metadata_uri` call sites.
- [`src/bffi_pipeline/stages/m8/runner.py`](../../src/bffi_pipeline/stages/m8/runner.py) — `ident/helmet/`, `helmet/`, `HELMET_MAP_FILENAME` call sites.
- [`src/bffi_pipeline/stages/m9/ysa_disambiguation_report.py`](../../src/bffi_pipeline/stages/m9/ysa_disambiguation_report.py) — `_HELMET_IDENT_PREFIX` (becomes computed from `LibrarySource` set).
- [`src/bffi_pipeline/provenance/writer.py`](../../src/bffi_pipeline/provenance/writer.py) and [`src/bffi_pipeline/provenance/logger.py`](../../src/bffi_pipeline/provenance/logger.py) — Activity emission sites for the renamed `bffi-prov:bibId` + new `bffi-prov:sourceCode` predicates.
- `CLAUDE.md` § "Committed identifiers" — the FI-HELME URI defaults Phase B preserves byte-for-byte; the bffi-prov v2 predicate rename lands in the same commit that updates this section.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 — the bffi-prov vocabulary reference doc (per `CLAUDE.md`'s pointer); Phase B documents the v2 predicate set there.

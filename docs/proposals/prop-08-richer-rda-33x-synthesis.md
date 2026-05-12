# P-08 — Richer RDA 33X synthesis from leader / 007 / 008 / 245$h

**Status**: proposed.
**Scope**: 2-4 days (depending on how many signal layers we land in one batch).
**Proposal-base commit**: `46b0f8a`. The "Motivation" reasons about
`src/marcxml_export_pipeline/sierra/itype_to_rda.py` and
`src/marcxml_export_pipeline/sierra/marcxml.py` immediately after the
bib-material-code-as-primary-signal commit. If `main` has moved before
this is acted on, re-verify with
`git diff 46b0f8a..HEAD --
src/marcxml_export_pipeline/sierra/itype_to_rda.py
src/marcxml_export_pipeline/sierra/marcxml.py
src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql
src/bffi_pipeline/validation/marcxml.py`.

## Motivation

P-02's 2026-05-12 5 000-record production-style run dropped **525
records (10.5 %)** at the M2 ``marcxml-content-minimum`` gate solely on
missing RDA 336/337/338. The follow-up work in `3f92a09` +
`46b0f8a` added a two-signal synth path:

1. **Bib-level** `bib_record_property.material_code` (authoritative
   for the manifestation, ~12 mapped codes).
2. **Item-level** `item_record.itype_code_num` fallback
   (~47 mapped itypes; matches `MIN(itype_code_num)` on bibs with mixed
   itypes).

This recovers the majority of the 525 dropped records, but two failure
modes remain:

- **No mapped signal**: `material_code` is `NULL` / unmapped (sample-
  thin codes `a` / `c` / `x`), AND none of the bib's items carry a
  mapped itype (or the bib has no items at all). These records still
  drop on the M2 gate.
- **Coarse-grained tuple**: even when a signal is mapped, we pick a
  single coarse tuple per code. ``material_code="3"`` (CD music) maps
  to performed-music / audio / audio-disc — but the *same* code
  covers 1 649 spoken-word CDs that should be spoken-word / audio /
  audio-disc. The current cascade gets those right only because
  spoken-word records were typically cataloguer-coded already.

MARC carries several **more precise** signals that we read into the
record but ignore for 33X synthesis:

- **Leader/06 (record type)** + **leader/07 (bib level)** —
  authoritative one-character code disambiguating language material
  (`a`) vs notated music (`c` / `d`) vs musical sound recording
  (`j`) vs nonmusical sound recording (`i`) vs cartographic (`e` /
  `f`) vs video (`g`) vs computer file (`m`) vs 3-D artifact (`r`).
  Maps near-1-to-1 to RDA 336 content type, and stronger than
  `material_code` because the leader is the leader. The B1 music
  exemption already keys off this (leader/06 ∈ {c, d, i, j}).
- **007 physical-description fixed-field** — the cleanest direct
  mapping to RDA 338 carrier we have. 007/00 category code maps
  deterministically: `s` + 007/01=`d` → audio disc, `s` + 007/01=`s`
  → audio cassette, `v` + 007/04=`v` → videodisc, `v` + 007/04=`s`
  → videocassette, `c` + 007/01=`o` → optical computer disc, `t` +
  007/01=`a` → regular print volume, `e` + 007/01=`d` → cartographic
  atlas, etc. Helmet records that already have 007 (most post-RDA
  bibs and many pre-RDA bibs) could derive **carrier + media**
  deterministically rather than via the material-code vote.
- **008 fixed-field, material-specific positions** — for sound
  recordings (008/18-34), 008/06 disambiguates 12-inch / 16-inch /
  cassette etc.; for videos, 008/29 = form of item (videocassette /
  videodisc / online resource); for maps, 008/25 = type of
  cartographic material.
- **006 additional material characteristics** — disambiguates mixed-
  material bibs (e.g. a videogame packaged with a book) where leader
  alone is ambiguous.
- **245$h (GMD: general material designation)** — what pre-RDA
  cataloguers used instead of 33X. Free text but conventionalised
  (`[äänilevy]`, `[videotallenne]`, `[elektroninen aineisto]`,
  `[nuotti]`, etc.). The pre-RDA records we're trying to recover are
  exactly the records that carry 245$h instead of 33X. Trivial regex
  classifier.
- **300$a (extent)** — last-resort textual: "1 CD", "1 DVD-levy",
  "1 LP-levy", "1 äänikasetti", "kuvateos", etc. Use only when no
  fixed-field signal is present.

Expected uplift on the 525 dropped: most carry **at least 007 or
245$h**. A 007-based pass alone should lift recovery from the current
"all-or-nothing per material code" to near-deterministic per record.

## Approach

A **cascade** with explicit priority order, evaluated only when no
cataloguer-coded 33X is present (the existing
`if not present_tags & {"336", "337", "338"}` gate stays):

| Pri | Signal | Decides | Mapping shape |
|---|---|---|---|
| 1 | cataloguer-coded 33X | all three | accept verbatim (existing) |
| 2 | 007 (per category code 0-1) | 337 media + 338 carrier | small table per category |
| 3 | leader/06 | 336 content | one-char dict |
| 4 | 008 material-specific positions | refine 338 carrier from broad 007 categories | per-leader/06-class table |
| 5 | 006 | tie-break mixed-material bibs | per-006/00 category table |
| 6 | 245$h GMD regex | fill any still-missing slot | Finnish + English token set |
| 7 | bib `material_code` | coarse fallback for any still-missing slot | existing `MATERIAL_TO_RDA` |
| 8 | item `itype_code_num` | coarse fallback (no items → skip) | existing `ITYPE_TO_RDA` |
| 9 | 300$a regex | last-resort textual | Finnish extent vocabulary |

Each signal contributes **independently per RDA slot** (336 / 337 /
338) — a high-priority signal that resolves only one slot doesn't
block a lower-priority signal from resolving the others. The synth
helper composes a final `RdaCarrier` from the slot-wise winners. If
any slot is still empty after running the cascade, no 33X is
synthesised and the M2 gate drops the bib (preserving strict
behaviour when no signal speaks for the record).

Implementation shape:

- New module `src/marcxml_export_pipeline/sierra/rda_signals.py`
  housing one resolver per signal layer, each typed
  `(record: RecordContext) → PartialRda` where `PartialRda` is the
  RDA-tuple-with-optional-slots dataclass.
- `RecordContext` is a thin view over the streamed row exposing
  leader, control fields (006/007/008), datafields (245$h, 300$a),
  bib `material_code`, and the items list. Lazy-decoded from the
  existing row tuple so non-synth code paths don't pay the cost.
- `lookup_rda` becomes `resolve_rda(record_context)`, which runs
  the cascade and returns either a complete `RdaCarrier` or `None`.
- Existing `MATERIAL_TO_RDA` / `ITYPE_TO_RDA` tables stay; they
  become layers 7-8 in the cascade rather than the primary path.

## Prerequisites

- `46b0f8a` shipped (bib-material code as primary signal). P-08 is
  a quality improvement on top.
- A coverage analysis pass that:
  1. tallies the 525 dropped records by which signals they
     carry (leader/06, 007, 008, 245$h, 300$a present / absent);
  2. shows the cascade's expected recovery rate slot-by-slot.
  This determines whether we ship the full cascade or stop at the
  top-N highest-yield layers. Lives under
  `scratchpad/rda-signal-coverage/`.
- Gold cases: at least two pre-RDA records per signal-resolved
  path so a regression in the cascade is caught at test time.
  Reusable as M2 boundary fixtures.

## Risks

- **Mis-mapping in 007 / 008 tables**: MARC 007 is dense, with
  category-specific subfield positions; getting the position
  offsets wrong on (say) the `s` audio family would mis-tag every
  audio bib in the corpus. Mitigated by unit tests against the LoC
  007 examples (LoC publishes canonical sample 007s per category)
  and by a deliberately small initial set of category codes (audio
  + video + computer file + text — these cover ~95 % of Helmet).
  Add more categories only when cataloguer-validated.
- **Signal disagreement**: 007 says "videodisc" but 245$h says
  "äänilevy". The cascade's priority order resolves this (007 wins),
  but a disagreement is interesting enough to log to provenance
  rather than silently picking one. Out-of-scope for synth-at-export
  but worth surfacing as a metric.
- **Pre-RDA records with stale 007**: a record migrated from a much
  older system may carry a 007 that no longer reflects the
  manifestation (e.g. cassette 007 on a record that's been
  re-issued as CD). No good way to detect this from MARC alone;
  cataloguer-coded 33X is the only fix. Accept that the cascade
  improves the bulk recovery and leaves the rare migration-stale
  case to manual cleanup.
- **GMD regex brittleness**: 245$h Finnish vocabulary is
  conventionalised but not standardised. Build the regex against
  the actual Helmet corpus (sample query against
  `varfield WHERE marc_tag='245' AND content LIKE '%[%]%'`) rather
  than from theoretical examples; expect ongoing additions as new
  GMDs surface.
- **Performance**: the cascade reads more of each row than the
  current synth. Worth profiling — the synth runs per-row in
  `build_marcxml_for_row` workers, so a per-row regression would
  show up in the 5k smoke baseline.

## Open questions

- **How much does the cascade actually recover on the 525?** The
  prerequisite coverage analysis settles this. If 007 alone
  recovers 80 % of the 525, layers 4-6 may not be worth shipping.
- **Should leader/06 always set 336 content, or only when nothing
  else has?** Strict priority (leader wins) vs slot-wise wins
  (a per-record 007 disagreeing with leader/06 is suspicious; log
  and prefer leader). Lean toward strict priority — leader is
  authoritative for the record type by MARC definition.
- **Counterpoint — is the current 525-drop rate actually a
  problem?** Cataloguer guidance on the 525 was "drop these for
  now, we'll re-catalogue the pre-RDA backlog over time". If
  Helmet is happy with `10.5 %` drop, P-08 stays `proposed` and
  the cataloguers re-code 33X at source. The case for action
  is strongest if the 525 contains *load-bearing* records the
  downstream consumers expect to find in Skosmos.
- **Alternative — make the M2 ``marcxml-content-minimum`` gate
  relax for pre-RDA records and synthesise 33X at M3 (BIBFRAME)
  instead of M2 (MARCXML)?** Synthesising at M3 has access to the
  marc2bibframe2-lifted `bf:Instance` carrier hints, which are
  themselves derived from MARC 33X / 007 / 008 / leader. Cleaner
  semantic layer but loses the export-side determinism (every M3
  run depends on the live XSLT submodule). Lean against —
  Sierra-export is the right boundary because the data lives in
  Sierra.
- **Configuration vs hard-coded**: should the cascade priority
  and signal-to-tuple mappings live in YAML (cataloguer-editable)
  or in Python? Lean Python for the moment — the mappings are
  type-safe, ruff-lintable, mypy-checkable, and small enough
  that a Python edit isn't a barrier. Revisit if cataloguers
  ask to edit without a PR.
- **Should the synthesised 33X be tagged in provenance?** Yes —
  a `$5` subfield (institution code) on the synthesised
  datafield, with value `FI-HELME/synth-v<N>`, lets downstream
  consumers distinguish cataloguer-coded from synth-coded 33X.
  Loses zero information; gains a clean audit trail. Currently
  the synth path emits no provenance marker.

## Cross-references

- [`src/marcxml_export_pipeline/sierra/itype_to_rda.py`](../../src/marcxml_export_pipeline/sierra/itype_to_rda.py)
  — current two-signal cascade; layers 7-8 of the proposed
  cascade. `ITYPE_TO_RDA` / `MATERIAL_TO_RDA` tables stay; the
  `lookup_rda` entry point gets renamed / wrapped.
- [`src/marcxml_export_pipeline/sierra/marcxml.py`](../../src/marcxml_export_pipeline/sierra/marcxml.py)
  `build_marcxml_for_row` synth gate — the
  `if not present_tags & {"336", "337", "338"}` check stays;
  the call to `lookup_rda(material_code, item_dicts)` is replaced
  by the new `resolve_rda(record_context)`.
- [`src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql`](../../src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql)
  — the streamed row already carries leader, varfields (including
  007/008 via the control-field subquery), 245, 300. No new SQL
  columns needed; the cascade reads what's already on the row.
- [`src/bffi_pipeline/validation/marcxml.py`](../../src/bffi_pipeline/validation/marcxml.py)
  M2 `marcxml-content-minimum` gate — the gate whose drop rate
  P-08 reduces. The 33X-required and the leader/06 music
  exemption logic stay; P-08 lifts records *over* the gate
  rather than weakening it.
- MARC 007 / 008 spec — LoC's published position tables are the
  source of truth for the per-category sub-mappings. Vendor
  them under `docs/` if we ship layers 2-4 so an offline build
  of the mapping stays reproducible.
- RDA Toolkit (Finnish translation) for the localised content /
  media / carrier labels — already used by `_TEXT_UNMEDIATED_VOLUME`
  etc.; extend as new tuples surface.

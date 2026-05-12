# P-08 — Richer RDA 33X synthesis from leader / 007 / 008 / 245$h

**Status**: backlog.
**Source proposal**: [`docs/proposals/prop-08-richer-rda-33x-synthesis.md`](../../proposals/prop-08-richer-rda-33x-synthesis.md)
at commit `19e09e4`.
**Plan-base commit**: `19e09e4`. To gauge drift before executing,
run `git diff 19e09e4..HEAD --
src/marcxml_export_pipeline/sierra/itype_to_rda.py
src/marcxml_export_pipeline/sierra/marcxml.py
src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql
src/bffi_pipeline/validation/marcxml.py
tests/unit/sierra/test_marcxml.py`.
**Phase commits**:

- Phase A (coverage analysis on the 525-drop list): `<unfilled>`
- Phase B (cascade scaffolding + leader/06 + 007 layers): `<unfilled>`
- Phase C (008 / 006 refinement + 245$h GMD + 300$a extent): `<unfilled>`
- Phase D (`$5` provenance marker + cataloguer-facing docs): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: 2-4 days total. Phase A is half a day (read-
only analysis against the existing 5k-run drop list); Phase B is one
day (the highest-yield layers, deterministic); Phase C is one day
(textual / fallback layers, more code paths); Phase D is half a day
(provenance + docs).

## Goal

Lift the M2 ``marcxml-content-minimum`` drop rate caused by missing
RDA 336/337/338 from the current cascade's coverage to "near-
deterministic per record that carries any MARC manifestation signal."

Concretely: on the same 5 000-record production-style sample that
P-02 ran on 2026-05-12, reduce the **525 drops on missing 33X** (the
current baseline after the bib-material + itype cascade in `3f92a09`
+ `46b0f8a`) to **≤ 100 drops** (~80 % recovery rate of the residual).
Remaining drops are records that genuinely carry no manifestation
signal at all and need cataloguer attention rather than synth.

## Definition of done

- All four phases have filled-in phase commits.
- The 5 000-record drop count on missing 33X is ≤ 100 (measured by
  re-running the same sample through M2 after the cascade ships).
- Every synthesised 33X datafield carries a `$5 FI-HELME/synth-v<N>`
  provenance marker so downstream consumers can distinguish
  cataloguer-coded from synth-coded.
- Unit tests cover at least one record per cascade layer (leader/06,
  007 audio / video / computer / text, 008 sound-recording form,
  006 mixed-material, 245$h GMD, 300$a extent, plus the existing
  material-code + itype layers).
- `docs/external-dependencies.md` records the cataloguer-facing
  expectation that synth-coded 33X is opt-out-able via cataloguer-
  coded 33X (existing behaviour, just documented).

## Current state

`3f92a09` shipped the item-level itype cascade; `46b0f8a` added the
bib-level material code as the primary signal with itype as fallback.
The synth path in `build_marcxml_for_row` looks like:

```python
if not present_tags & {"336", "337", "338"}:
    rda = lookup_rda(material_code, item_dicts)
    if rda is not None:
        for c_label, c_code in rda.content_types:
            varfields.append(_build_rda_varfield("336", c_label, c_code, "rdacontent"))
        varfields.append(_build_rda_varfield("337", rda.media[0], rda.media[1], "rdamedia"))
        varfields.append(_build_rda_varfield("338", rda.carrier[0], rda.carrier[1], "rdacarrier"))
```

`MATERIAL_TO_RDA` covers 12 alphanumeric codes (`1`-`9`, `g`, `h`,
`q`, `r`, `s`); `ITYPE_TO_RDA` covers 47 numeric itype codes. Bibs
whose `material_code` is unmapped (`a` / `c` / `x`) AND none of whose
items carry a mapped itype still drop. The 2026-05-12 5k run's exact
residual count is a Phase A deliverable.

The streamed SQL row already carries the signals P-08 needs (leader,
varfields incl. 245 / 300, controlfields incl. 006 / 007 / 008,
items, material_code). **No new SQL columns are required for P-08.**

## Strategy

A **slot-wise cascade** with explicit priority order. Each layer is
a function `(record_context) → PartialRda`. The composer runs layers
in priority order; each layer fills only the slots that are still
empty. A high-priority signal that resolves one slot (say 338
carrier) doesn't block lower-priority signals from filling the others
(336 content, 337 media). If after the full cascade any slot is still
empty, no 33X is synthesised and the M2 gate drops the bib —
preserving the strict behaviour when no signal speaks for the record.

Priority order (priority 1 = cataloguer-coded already wins via the
existing pre-gate; the cascade runs only when that gate doesn't fire):

| Pri | Signal | Slots it can fill |
|---|---|---|
| 2 | 007 (per category 0-1) | 337 media + 338 carrier |
| 3 | leader/06 | 336 content |
| 4 | 008 material-specific positions | 338 carrier refinement |
| 5 | 006 | tie-break on mixed-material bibs |
| 6 | 245$h GMD regex | any still-empty slot |
| 7 | bib `material_code` (existing) | any still-empty slot |
| 8 | item `itype_code_num` (existing) | any still-empty slot |
| 9 | 300$a regex | last-resort, any still-empty slot |

Phase B implements 2-3 (the highest-yield deterministic layers).
Phase C implements 4-6 and 9 (refinement + textual fallbacks). The
existing layers 7-8 are preserved unchanged; they slot into the
cascade rather than being the primary path.

---

## Phase A — Coverage analysis on the 525-drop list

Estimated wall-time: half a day. Read-only — no production code
changes; only adds a scratchpad analysis script.

### A1. Capture the current 525-drop list

Re-run the 2026-05-12 5k sample through M2 (or read the existing
drop log if it's still on disk) and write the list of bib IDs that
fail with `error_type="marcxml-content-minimum"` AND error message
mentioning `336/337/338` to `scratchpad/rda-cascade/drop-list.txt`.

```bash
uv run bffi-pipeline marcxml-validate \
    --input <5k-sample-dir> \
    --output-errors scratchpad/rda-cascade/m2-errors.jsonl
jq -r 'select(.error_type=="marcxml-content-minimum" and
    (.message|contains("336/337/338"))) | .helmet_bib_id' \
    scratchpad/rda-cascade/m2-errors.jsonl \
    > scratchpad/rda-cascade/drop-list.txt
```

### A2. Signal-coverage tally

Add `scratchpad/rda-cascade/coverage_tally.py` (kept under
`scratchpad/` because it's a one-shot analysis, not production code).
For each bib ID in `drop-list.txt`, read the streamed MARCXML and
record:

- leader/06 character;
- presence and 007/00-007/01 of each 007 control field;
- presence and 008/06 / 008/18 / 008/25 / 008/29 of 008;
- presence and content of any 245$h subfield (`xml.find('.//245/subfield[@code="h"]')`);
- presence and content of any 300$a subfield;
- `material_code` and the item-level itype histogram (already on row).

Output a histogram showing what fraction of the 525 carries each
signal, and a projected per-layer recovery rate.

### A3. Phase A acceptance

- [ ] `scratchpad/rda-cascade/drop-list.txt` exists with the 525
      bib IDs.
- [ ] `scratchpad/rda-cascade/coverage-report.md` summarises:
  - per-signal coverage on the 525,
  - projected recovery rate after each cascade layer,
  - the residual count after the full cascade (records carrying
    none of the signals — the genuine "cataloguer must re-code"
    cases).
- [ ] If the projected recovery is below ~80 %, **revise the
      goal or the layer set** before starting Phase B. The plan
      stays in `backlog/`; either iterate on the cascade design
      or document the empirical limit and re-scope.

---

## Phase B — Cascade scaffolding + leader/06 + 007

Estimated wall-time: one day. Implements the highest-yield
deterministic layers and the type infrastructure to slot the rest in.

### B1. Scaffold the cascade

Add `src/marcxml_export_pipeline/sierra/rda_signals.py`:

```python
@dataclass(frozen=True)
class PartialRda:
    """RDA 33X with optional per-slot resolution. The cascade
    composes a final RdaCarrier from slot-wise winners."""
    content_types: tuple[tuple[str, str], ...] | None = None
    media: tuple[str, str] | None = None
    carrier: tuple[str, str] | None = None

    def merge_below(self, other: PartialRda) -> PartialRda:
        """Layer this (higher-priority) on top of other (lower).
        Only fill the slots this layer left empty."""
        ...


@dataclass(frozen=True)
class RecordContext:
    """View over the streamed row, exposing the signals P-08 reads."""
    leader_dict: Mapping[str, Any] | None
    varfields: Sequence[Mapping[str, Any]]      # incl. 245, 300
    controlfields: Sequence[Mapping[str, Any]]  # incl. 006/007/008
    items: Sequence[Mapping[str, Any]]
    material_code: str | None


CascadeLayer = Callable[[RecordContext], PartialRda]


def resolve_rda(ctx: RecordContext, layers: Sequence[CascadeLayer]) -> RdaCarrier | None:
    """Run the cascade. Return a complete RdaCarrier or None if any
    slot is still empty after all layers."""
    ...
```

### B2. Layer 2 — 007 deterministic carrier

Add `from_marc_007(ctx) -> PartialRda` covering the four highest-
volume MARC 007 category codes:

- `s` (sound recording) — 007/01 ∈ {`d`: audio disc, `s`: audio
  cassette}, refined optionally by 008/06 (configuration).
- `v` (videorecording) — 007/04 ∈ {`v`: videodisc, `s`: videocassette,
  `f`: videocartridge}, media always `v`.
- `c` (computer/electronic resource) — 007/01 ∈ {`o`: optical disc,
  `r`: remote (online)}, media `c`, carrier `cd` or `cr` accordingly.
- `t` (text) — 007/01 = `a` → volume.

The content-type slot stays empty (007 doesn't carry that signal —
"is this audio music or audio speech?" needs leader/06 or 008).

### B3. Layer 3 — leader/06 content type

Add `from_leader_06(ctx) -> PartialRda` covering the leader/06
codes that map deterministically to RDA 336:

| leader/06 | 336 content | label |
|---|---|---|
| `a` | `txt` | teksti |
| `c` | `ntm` | nuottikirjoitus |
| `d` | `ntm` | nuottikirjoitus (käsikirjoitus) |
| `e` | `cri` | kartografinen kuva |
| `f` | `cri` | kartografinen kuva (käsikirjoitus) |
| `g` | `tdi` | kaksiulotteinen liikkuva kuva |
| `i` | `spw` | puhe |
| `j` | `prm` | esitetty musiikki |
| `k` | `sti` | pysähtynyt kuva |
| `m` | `cop` | tietokoneohjelma |
| `r` | `tdf` | kolmiulotteinen muoto |
| `t` | `txt` | teksti (käsikirjoitus) |

Note: leader/06=`o` (kit) is intentionally omitted; kits carry
multiple content types and need 006 to disambiguate (Phase C).

### B4. Wire the cascade into `build_marcxml_for_row`

Replace the `lookup_rda(material_code, item_dicts)` call with the
slot-wise composer:

```python
if not present_tags & {"336", "337", "338"}:
    ctx = RecordContext.from_row(...)
    rda = resolve_rda(ctx, layers=DEFAULT_LAYERS)
    if rda is not None:
        # emit as before
```

`DEFAULT_LAYERS` orders the existing material/itype lookups as
layers 7-8 (wrapped in `PartialRda`-returning adapters) so all
behaviour up to the new layers stays unchanged when the new
signals are absent.

### B5. Phase B acceptance

- [ ] `rda_signals.py` exists with `PartialRda`, `RecordContext`,
      `resolve_rda`, `from_marc_007`, `from_leader_06`, and adapter
      wrappers for the existing material/itype lookups.
- [ ] Unit tests cover at least one record per 007 category
      (s/v/c/t) and one per leader/06 code in the table above.
- [ ] **Regression**: every existing 33X-synth test in
      `tests/unit/sierra/test_marcxml.py` still passes. The
      cascade with only layers 7-8 enabled must reproduce the
      current behaviour exactly.
- [ ] Re-run on the 525-drop list (or a sampled subset): record
      the new recovery rate. Should match the Phase A projection
      within a few percent.

---

## Phase C — 008 / 006 / 245$h / 300$a layers

Estimated wall-time: one day. Adds the refinement and textual-
fallback layers.

### C1. Layer 4 — 008 material-specific carrier refinement

Add `from_marc_008(ctx) -> PartialRda` that, for leader/06 ∈
{`i`, `j`} (sound recordings), reads 008/06 (configuration of
playback channels) and 008/03 (speed) to distinguish 12-inch LP
(`6` material code) from 7-inch (still `6`). For leader/06=`g`
(video), reads 008/29 (form of item) to refine `vd` (videodisc)
vs `vc` (videocassette) when 007 disagrees.

This is a *refinement* layer — it sets the same slots layer 2 set,
but only when layer 2's answer needs sharpening (or when 007 is
absent and 008 carries the same data). The slot-wise composer
naturally handles this because layer 4 runs after layer 2 and the
slot is already filled — so layer 4 is a no-op in the "both layers
agree" path. To allow layer 4 to *override* layer 2 when 007 is
known-stale (rare migration cases), add an explicit
`override_carrier_when_008_is_authoritative` flag to `PartialRda`.

### C2. Layer 5 — 006 tie-break

Add `from_marc_006(ctx) -> PartialRda` for mixed-material bibs
where leader/06=`o` (kit) or where leader/06 conflicts with 007.
Reads 006/00 (form of material) using the same code-to-content
mapping as 008/00 / leader/06.

### C3. Layer 6 — 245$h GMD regex

Add `from_245_h_gmd(ctx) -> PartialRda` that parses 245$h text
(Finnish + English) into RDA slots:

| 245$h token | content | media | carrier |
|---|---|---|---|
| äänilevy / sound recording | prm or spw* | s | sd |
| äänikasetti / cassette | prm or spw* | s | ss |
| videotallenne / videorecording | tdi | v | vd |
| elektroninen aineisto / electronic resource | cop | c | cr |
| nuotti / music | ntm | n | nc |
| kartta / map | cri | n | nb |
| esine / object | tdf | n | nr |

*Token alone can't tell music from speech; this layer fills 336
only when leader/06 already says `i` (speech) or `j` (music).

Build the token list against the actual Helmet 245$h corpus:

```sql
SELECT DISTINCT content
FROM sierra_view.subfield sf
JOIN sierra_view.varfield vf ON sf.varfield_id = vf.id
WHERE vf.marc_tag = '245' AND sf.tag = 'h'
ORDER BY content;
```

Stash the result in `scratchpad/rda-cascade/245h-corpus.txt`;
the regex should cover ≥ 95 % of distinct tokens.

### C4. Layer 9 — 300$a extent regex

Add `from_300_a_extent(ctx) -> PartialRda`. Last-resort textual
fallback for records with no fixed-field signals at all (legacy
imports from much older systems). Same shape as layer 6 but
keys on 300$a tokens ("1 CD", "1 DVD-levy", "1 LP-levy",
"1 äänikasetti", "1 kirja", "1 kuvateos", "1 kartta", "1 esine").

### C5. Phase C acceptance

- [ ] All four new layer functions exist with unit tests.
- [ ] Each layer is independently tunable (separate function, separate
      table, separate test file).
- [ ] Re-run on the 525-drop list: residual ≤ 100 records (the
      plan goal).
- [ ] Conflict-logging: when 007 and 008 disagree on carrier, the
      cascade composer writes a `scratchpad/rda-cascade/conflicts.jsonl`
      entry. Not surfaced as a failure — informational for cataloguers.

---

## Phase D — Provenance marker + cataloguer-facing docs

Estimated wall-time: half a day.

### D1. `$5 FI-HELME/synth-v<N>` on synthesised datafields

Extend `_build_rda_varfield` to accept a `source_marker` arg and
emit a `$5` subfield carrying the institution code + synth version:

```xml
<datafield tag="336" ind1=" " ind2=" ">
  <subfield code="a">teksti</subfield>
  <subfield code="b">txt</subfield>
  <subfield code="2">rdacontent</subfield>
  <subfield code="5">FI-HELME/synth-v1</subfield>
</datafield>
```

MARC convention: `$5` is "institution to which field applies". The
Helmet agency code is `FI-HELME` (already in `AGENCY_CODE`); the
`/synth-v<N>` suffix carries the cascade version so a future
re-cascading can find synth-v1 fields and replace them deterministically
without disturbing cataloguer-coded fields.

### D2. Cataloguer-facing docs

Update `docs/external-dependencies.md` with a new section:

- The pipeline synthesises RDA 33X for pre-RDA records that
  carry MARC manifestation signals but no cataloguer-coded
  33X. Synth fields are marked `$5 FI-HELME/synth-v<N>`.
- Cataloguers can opt out per-record by adding cataloguer-
  coded 33X to the Sierra record — the synth path only fires
  when the bib carries none of `{336, 337, 338}`.
- Synth versions are documented in
  `src/marcxml_export_pipeline/sierra/rda_signals.py` next to the
  `SYNTH_VERSION` constant; bumping the version is a notice to
  re-cascade existing synth records on the next full export.

### D3. Phase D acceptance

- [ ] `$5 FI-HELME/synth-v1` lands on every synthesised 33X
      datafield in the next full run.
- [ ] `docs/external-dependencies.md` documents the marker and
      the cataloguer opt-out.
- [ ] A test fixture confirms cataloguer-coded 33X (no `$5`
      marker) still passes through unchanged when present.
- [ ] Plan moves to `completed/` via `git mv` in the Phase D
      commit; corresponding cross-references in
      `docs/proposals/README.md`, `docs/plans/README.md`, and any
      source comments referencing the plan path are updated in
      the same commit.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase A shows the projected recovery is well below the 80 % goal | Medium | Plan stays in `backlog/`; iterate on layer design or re-scope the goal. The plan is structured so Phase A can answer "is this worth doing" before sinking Phase B-C effort. |
| Mis-mapping in 007/008 position tables (especially the sound-recording family — dense subfield positions) | Medium-high | Unit tests against LoC's canonical sample 007s per category. Initial layer set covers only the four highest-volume category codes (s/v/c/t); extend only with cataloguer-validated additions. |
| 007 / 008 / leader/06 disagree on the same record | Medium | Slot-wise priority resolves this (higher-priority wins). Disagreements are logged to `scratchpad/rda-cascade/conflicts.jsonl` so cataloguers can review patterns even though synth doesn't fail. |
| 245$h regex misses Finnish GMDs not in the initial token list | Medium | Build the token list against the actual Helmet 245$h corpus (Phase C3) rather than from theoretical examples. Add a CI check that flags new GMD tokens appearing in production runs. |
| `$5 FI-HELME/synth-v<N>` is unexpected by downstream M2/M3 stages | Low | `$5` is a standard MARC subfield. Downstream stages already pass-through unknown subfields. Smoke-test on the 5k sample after Phase D. |
| Performance regression in per-row synth | Low-medium | The cascade reads more of each row but the data is already on the row (no extra DB / IO). Profile against the P-02 5k baseline; if regression > 10 % per-row, optimise the layer pipeline (cached `RecordContext` factory, early-exit when all slots fill, etc.). |
| Pre-RDA records with stale 007 (cassette 007 on a record re-issued as CD) get mis-tagged | Low | Accept — the cascade improves bulk recovery; rare migration-stale cases are noise on the order of cataloguer-correction throughput anyway. The provenance marker (Phase D) makes these correctable in bulk by re-cascading after the cataloguer flags the pattern. |

## Rollback

Each phase ships behind a small commit; rollback is `git revert`
on the phase commit:

- **Phase D rollback**: removes the `$5` marker. No data loss; the
  next full export will re-emit synth 33X without the marker.
- **Phase C rollback**: removes the 008/006/245$h/300$a layers.
  Cascade falls back to layers 2-3 + the existing material/itype.
  Drop count rises from "≤ 100" back toward "≤ ~200" (depending on
  Phase A's signal distribution).
- **Phase B rollback**: removes the cascade scaffolding and the
  leader/06 + 007 layers. Cascade falls back to the existing
  `lookup_rda(material_code, item_dicts)` — the post-`46b0f8a`
  behaviour, ~525-record baseline.
- **Phase A rollback**: scratchpad-only; no production code change
  to revert. Just delete the scratchpad directory.

The full revert path keeps the synth path functional throughout —
no phase introduces a behaviour the previous phase can't fall back to.

## Open issues to close before / during execution

- **Where does `RecordContext` live?** Sketched in `rda_signals.py`
  for cohesion with the layers. If a future use case wants the same
  view outside of synth (e.g. for the M2 validation gate itself),
  promote it to `marcxml_export_pipeline/sierra/dtos.py` alongside
  `Leaderfield` / `Varfield` / `Subfield`.
- **Should `SYNTH_VERSION` be a code constant or git-derived?**
  Code constant — easier to bump intentionally, easier for
  cataloguers to verify in MARC, no surprises in tests. Increment
  manually when the cascade's output for an existing record would
  change.
- **Counterpoint — is 525-down-to-100 enough to justify 2-4 days?**
  The drop rate matters mainly insofar as cataloguers care about
  the missing records being in Skosmos. If Helmet is fine with the
  10.5 % residual being re-coded over time, P-08 stays in `backlog/`
  and the cataloguers proceed. The plan exists so the action is
  defined when the priority crystallises, not because the action is
  guaranteed to ship.
- **Cross-product with P-07 (856-as-Item)**: P-07 also touches M2
  routing. Phase D's `$5` marker is independent (additive subfield
  on the synth 33X datafield, nothing P-07 touches), so the two
  plans can ship in either order.

## Cross-references

- [`docs/proposals/prop-08-richer-rda-33x-synthesis.md`](../../proposals/prop-08-richer-rda-33x-synthesis.md)
  — graduated source proposal. Stays in place as a `planning
  (graduated)` stub once this plan lands.
- [`src/marcxml_export_pipeline/sierra/itype_to_rda.py`](../../../src/marcxml_export_pipeline/sierra/itype_to_rda.py)
  — `ITYPE_TO_RDA` and `MATERIAL_TO_RDA` stay as cascade layers
  7-8. `lookup_rda` is wrapped into `PartialRda`-returning adapters
  in Phase B4.
- [`src/marcxml_export_pipeline/sierra/marcxml.py`](../../../src/marcxml_export_pipeline/sierra/marcxml.py)
  `build_marcxml_for_row` — call site replaced in Phase B4.
- [`src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql`](../../../src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql)
  — already streams the signals P-08 needs (leader, 245, 300, 006,
  007, 008 via the controlfield subquery, items, material_code).
  No SQL changes required.
- [`src/bffi_pipeline/validation/marcxml.py`](../../../src/bffi_pipeline/validation/marcxml.py)
  `marcxml-content-minimum` gate — the gate whose drop rate P-08
  reduces. Gate logic unchanged; P-08 lifts records *over* the
  gate rather than weakening it.
- [`docs/external-dependencies.md`](../../external-dependencies.md)
  — updated in Phase D2 with cataloguer-facing notes on the synth
  marker and the opt-out.
- [`docs/performance/`](../../performance/) — Phase A and Phase
  C5's drop-count measurements get a short snapshot here so the
  recovery is auditable against the baseline.
- LoC MARC 007 / 008 spec — source of truth for the per-category
  position tables. Cite specific section numbers in the layer
  docstrings; vendor a copy under `docs/` if the layer set
  expands beyond the initial four categories.

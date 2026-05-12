# P-08 — Richer RDA 33X synthesis from leader / 008 / 007

**Status**: in-progress (Phase A landed; layer set revised before Phase B).
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

- Phase A (coverage analysis + layer-set revision): `<unfilled>`
- Phase B (cascade scaffolding + (leader/06, 008-form) layer + 007 refinement): `<unfilled>`
- Phase C (300$a extent fallback + adapter layers for existing material/itype): `<unfilled>`
- Phase D (`$5` provenance marker + cataloguer-facing docs): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: 2-3 days remaining. Phase A is done (this
commit). Phase B is one day (the cascade scaffolding + the universal
(leader/06, 008-form) layer that covers 100 % of current drops, plus
007 refinement). Phase C is half a day (extent fallback + adapter
wrapping for material/itype). Phase D is half a day (provenance + docs).

## Goal

Lift the M2 ``marcxml-content-minimum`` drop rate caused by missing
RDA 336/337/338 from the current cascade's coverage to "near-
deterministic per record that carries any MARC manifestation signal."

Concretely: on the same 5 000-record production-style sample that
P-02 ran on 2026-05-12, reduce the **566 drops on missing 33X**
(measured by Phase A — see [`scratchpad/rda-cascade/coverage-report.md`](../../../scratchpad/rda-cascade/coverage-report.md))
to **≤ 50 drops**. The Phase A coverage analysis projects this is
feasible because **100 % of current drops carry both a mapped
leader/06 AND an 008 controlfield** — the cascade reads both, fills
all three RDA slots from their combination, and only fails on records
whose leader/06 is also unmapped (extremely rare in Helmet).

## Definition of done

- All four phases have filled-in phase commits.
- The 5 000-record drop count on missing 33X is ≤ 50 (measured by
  re-running the same sample through M2 after the cascade ships, or
  by re-exporting from Sierra with the cascade active and walking
  the result).
- Every synthesised 33X datafield carries a `$5 FI-HELME/synth-v<N>`
  provenance marker so downstream consumers can distinguish
  cataloguer-coded from synth-coded.
- Unit tests cover at least one record per cascade layer (007
  audio / video / computer / text; (leader/06, 008-form) for each of
  the four dominant leader/06 codes a/g/m/e; 300$a extent fallback;
  plus the existing material-code + itype layers preserved as
  adapters).
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

`MATERIAL_TO_RDA` covers 12 alphanumeric codes; `ITYPE_TO_RDA` covers
47 numeric itype codes. The 2026-05-12 5k sample on disk was emitted
*before* either landed; Phase A measures 566 records still dropping
on missing 33X under the pre-synth MARCXML.

The streamed SQL row already carries the signals P-08 needs (leader,
varfields incl. 245 / 300, controlfields incl. 006 / 007 / 008,
items, material_code). **No new SQL columns are required for P-08.**

## Strategy (revised after Phase A)

A **slot-wise cascade** with explicit priority order. Each layer is
a function `(record_context) → PartialRda`. The composer runs layers
in priority order; each layer fills only the slots that are still
empty. A high-priority signal that resolves one slot doesn't block
lower-priority signals from filling the others. If after the full
cascade any slot is still empty, no 33X is synthesised and the M2
gate drops the bib — preserving the strict behaviour when no signal
speaks for the record.

Priority order (priority 1 = cataloguer-coded already wins via the
existing pre-gate; the cascade runs only when that gate doesn't fire):

| Pri | Signal | Slots it can fill | Drop-list coverage |
|---|---|---|---:|
| 2 | 007 with category ∈ {s, v, c, t} | 337 media + 338 carrier (specific) | 5.3 % |
| 3 | **(leader/06, 008-form) lookup** | 336 + 337 + 338 (universal default) | 100.0 % |
| 4 | bib `material_code` adapter (existing) | any still-empty slot | unknown* |
| 5 | item `itype_code_num` adapter (existing) | any still-empty slot | unknown* |
| 6 | 300$a extent regex | last-resort, any still-empty slot | 17.1 % |

*Material/itype coverage is unknown from MARCXML alone; they're
preserved as adapter layers for records the (leader/06, 008-form)
table doesn't cover (e.g. exotic leader/06 values, missing 008).

Phase B implements priorities 2 + 3 (the universal default + 007
refinement). Phase C implements priorities 4-6 (preserved adapters
+ extent fallback).

### Deferred layers (no yield on the 5k sample)

Phase A measured **0 %** drop-list coverage for these signals.
They're deferred to a future iteration if a larger corpus shows
non-trivial yield:

- **245$h GMD regex** — 0 % of drops carry 245$h. Pre-RDA Helmet
  records simply don't use GMDs.
- **006 controlfield** — 0 % of drops carry 006. Helmet doesn't
  use 006 outside of multi-medium kits, which don't appear in this
  sample's drop list.

---

## Phase A — Coverage analysis + layer-set revision (DONE)

Phase A walked the 2026-05-12 5k sample at `data/sample-5k-marcxml/`
via `scratchpad/rda-cascade/coverage_tally.py` and produced:

- `scratchpad/rda-cascade/drop-list.txt` — 566 bib IDs, one per line.
- `scratchpad/rda-cascade/coverage-report.md` — per-signal histograms
  and a per-layer projected recovery.

### Key findings

| Metric | Value | Implication |
|---|---:|---|
| Records dropping on missing 33X | 566 / 5 000 | Baseline; ~11 % of corpus |
| Drop-list leader/06 = `a` (text) | 452 (80 %) | Books dominate |
| Drop-list leader/06 = `g` (video) | 90 (16 %) | Video the second mass |
| Drop-list with **leader/06 mapped** | 566 (100 %) | Universal signal for 336 |
| Drop-list with **008 controlfield** | 566 (100 %) | Universal signal for 337/338 |
| Drop-list with usable 007 | 30 (5 %) | Refinement only |
| Drop-list with 245$h GMD token | 0 (0 %) | **Layer deferred** |
| Drop-list with 006 | 0 (0 %) | **Layer deferred** |
| Drop-list with 300$a extent token | 97 (17 %) | Useful last-resort |

### Why the original layer design underperformed

The original plan layered leader/06 (for 336) and 007 (for 337/338)
as separate slot-fillers. But **only 5 % of drops carry 007**, so
80 % of drops would have their 336 filled (by leader/06) and their
337/338 still empty — they'd still drop on the M2 gate. Projected
residual under the original layer design: **464 / 566 (82 %)**, well
above the plan's `≤ 100` target.

### Revised layer-set decision

The (leader/06, 008-form) signal is present on **100 %** of current
drops. Both signals are MARC-spec fixed-fields with well-defined
semantics:

- Leader/06 conveys the content type (`a` text, `g` video, `m`
  computer file, `e` map, `k` 2-D graphic, `r` 3-D object, etc.).
- 008 carries a "Form of item" position (different position per
  leader/06 class) that refines the carrier when the manifestation
  is non-default (large print, braille, microform, online).

Folded together as a single layer keyed on `(leader/06, 008-form)`,
they fill all three RDA slots with the canonical Helmet default
when 008-form is unset (~95 % of pre-RDA records) and refine the
carrier when 008-form is coded (large print, braille, etc.).

Re-projected residual under the revised design: **near 0** on this
sample. The plan's goal updates from `≤ 100` to `≤ 50`.

### Phase A acceptance

- [x] `scratchpad/rda-cascade/drop-list.txt` exists with 566 bib IDs.
- [x] `scratchpad/rda-cascade/coverage-report.md` summarises per-signal
      coverage and per-layer projected recovery rate.
- [x] The layer set has been revised (this commit) to reflect what
      Phase A found.

---

## Phase B — Cascade scaffolding + (leader/06, 008-form) + 007 refinement

Estimated wall-time: one day. Implements the universal default layer
and the 007 refinement layer.

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
    controlfields: Sequence[Mapping[str, Any]]  # incl. 007/008
    items: Sequence[Mapping[str, Any]]
    material_code: str | None


CascadeLayer = Callable[[RecordContext], PartialRda]


def resolve_rda(ctx: RecordContext, layers: Sequence[CascadeLayer]) -> RdaCarrier | None:
    """Run the cascade. Return a complete RdaCarrier or None if any
    slot is still empty after all layers."""
    ...
```

### B2. Layer 2 — 007 refinement

Add `from_marc_007(ctx) -> PartialRda` covering the four MARC 007
category codes present on this corpus (5 % of drops, but every
record where 007 disagrees with the leader+008 default is a record
where the cataloguer was being deliberately specific):

- `s` (sound recording) — 007/01 ∈ {`d`: audio disc, `s`: audio
  cassette}; media `s`.
- `v` (videorecording) — 007/04 ∈ {`v`: videodisc, `s`: videocassette,
  `f`: videocartridge}; media `v`.
- `c` (computer/electronic resource) — 007/01 ∈ {`o`: optical disc,
  `r`: remote (online)}; media `c`, carrier `cd` or `cr` accordingly.
- `t` (text) — 007/01 = `a` → volume; 007/01 = `b` → large-print
  volume; media `n`.

This layer fills 337+338 only (the content-type slot stays empty;
filled by layer 3 below).

### B3. Layer 3 — (leader/06, 008-form) universal default

Add `from_leader_and_008(ctx) -> PartialRda` keyed on the
`(leader/06, 008-form)` tuple. 008-form lives at a different
position per leader/06 class — encode the position map and the
tuple values together:

```python
# 008 "Form of item" position per leader/06 class. Per LoC MARC 21
# Bibliographic spec § 008.
_LEADER_06_TO_008_FORM_POS: Final[dict[str, int]] = {
    "a": 23, "t": 23,                # books / manuscripts
    "g": 29, "k": 29, "o": 29, "r": 29,  # visual materials, kits, 3-D
    "e": 29, "f": 29,                # cartographic
    "m": 23,                         # computer files
    "c": 23, "d": 23,                # notated music — refines carrier rarely
    "i": 23, "j": 23,                # sound recordings
}

# (leader/06, 008-form) → full RDA tuple. 008-form = ' ' (space) or
# 'r' (regular print reproduction) maps to the leader/06's *default*
# manifestation; specific forms (large print, braille, microform,
# online) override the carrier.
_LEADER_008_TO_RDA: Final[dict[tuple[str, str], RdaCarrier]] = {
    # leader/06 = 'a' (language material — books). Default is print
    # volume; 008/23 refines.
    ("a", " "): _TEXT_UNMEDIATED_VOLUME,
    ("a", "r"): _TEXT_UNMEDIATED_VOLUME,  # regular print reproduction
    ("a", "d"): RdaCarrier(  # large print
        content_types=(("teksti", "txt"),),
        media=("käytettävissä ilman laitetta", "n"),
        carrier=("isotekstinen nide", "nc"),
    ),
    ("a", "f"): RdaCarrier(  # braille
        content_types=(("taktiili teksti", "tct"),),
        media=("käytettävissä ilman laitetta", "n"),
        carrier=("nide", "nc"),
    ),
    ("a", "a"): RdaCarrier(  # microfilm
        content_types=(("teksti", "txt"),),
        media=("mikromuoto", "h"),
        carrier=("mikrofilmirulla", "hf"),
    ),
    ("a", "b"): RdaCarrier(  # microfiche
        content_types=(("teksti", "txt"),),
        media=("mikromuoto", "h"),
        carrier=("mikrofilmikortti", "he"),
    ),
    ("a", "o"): RdaCarrier(  # online
        content_types=(("teksti", "txt"),),
        media=("tietokonekäyttöinen", "c"),
        carrier=("verkkoaineisto", "cr"),
    ),
    ("a", "s"): RdaCarrier(  # electronic (same as online for Helmet)
        content_types=(("teksti", "txt"),),
        media=("tietokonekäyttöinen", "c"),
        carrier=("verkkoaineisto", "cr"),
    ),
    # leader/06 = 'g' (video). Default is videodisc (Helmet's
    # dominant pre-RDA video manifestation); 008/29 refines.
    ("g", " "): _VIDEO_VIDEODISC,
    ("g", "d"): _VIDEO_VIDEODISC,
    ("g", "o"): RdaCarrier(  # online video
        content_types=(("kaksiulotteinen liikkuva kuva", "tdi"),),
        media=("tietokonekäyttöinen", "c"),
        carrier=("verkkoaineisto", "cr"),
    ),
    # leader/06 = 'm' (computer file). Default is online.
    ("m", " "): RdaCarrier(
        content_types=(("tietokoneohjelma", "cop"),),
        media=("tietokonekäyttöinen", "c"),
        carrier=("verkkoaineisto", "cr"),
    ),
    ("m", "o"): RdaCarrier(
        content_types=(("tietokoneohjelma", "cop"),),
        media=("tietokonekäyttöinen", "c"),
        carrier=("verkkoaineisto", "cr"),
    ),
    # leader/06 = 'e' (cartographic — maps). Default is sheet.
    ("e", " "): _MAP_SHEET,
    # leader/06 = 'k' (two-dimensional non-projected graphic).
    ("k", " "): RdaCarrier(
        content_types=(("stillkuva", "sti"),),
        media=("käytettävissä ilman laitetta", "n"),
        carrier=("arkki", "nb"),
    ),
    # leader/06 = 'r' (three-dimensional artifact).
    ("r", " "): RdaCarrier(
        content_types=(("kolmiulotteinen muoto", "tdf"),),
        media=("käytettävissä ilman laitetta", "n"),
        carrier=("objekti", "nr"),
    ),
    # (leader/06 = 'c'/'d' notated music, 'i'/'j' sound recordings —
    # the M2 gate exempts these from 33X-required, so they don't
    # appear in the drop list. Mappings can be added later if a
    # future scope expansion needs them.)
}


def from_leader_and_008(ctx: RecordContext) -> PartialRda:
    """Universal default RDA tuple keyed on (leader/06, 008-form).
    Falls back to leader/06-only when 008-form is unmapped."""
    ...
```

When `(leader/06, 008-form)` is a key in the table, return the full
RDA tuple. When 008-form is *coded* but not in the table for that
leader/06 (rare migration cases), fall back to `(leader/06, " ")` —
the default for that content type. When leader/06 itself is unmapped,
return `PartialRda()` (empty; lower-priority layers handle it).

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

`DEFAULT_LAYERS = (from_marc_007, from_leader_and_008, ...)`.
Phase C adds the adapter layers + 300$a fallback below these two.

### B5. Phase B acceptance

- [ ] `rda_signals.py` exists with `PartialRda`, `RecordContext`,
      `resolve_rda`, `from_marc_007`, `from_leader_and_008`.
- [ ] Unit tests cover:
  - one record per 007 category (s/v/c/t);
  - one record per (leader/06, 008-form) tuple in the table above
    (a/`r`, a/`d`, a/`f`, a/`a`, a/`b`, a/`o`, g/` `, g/`o`,
    m/` `, e/` `, k/` `, r/` `);
  - the 007-overrides-008 case (007=`s`+`d` audio disc beats
    leader=`j`+008-form-default music CD — but j is music-exempt, so
    pick a leader that's in the gate, e.g. 007=`v`+`v` videodisc beats
    a hypothetical g/`o` online if 007 says otherwise);
  - the empty case (unmapped leader/06 + no 007 + no other signal).
- [ ] **Regression**: every existing 33X-synth test in
      `tests/unit/sierra/test_marcxml.py` still passes. Specifically,
      the bib-material-code-wins-over-items test needs to be
      re-evaluated: in the revised cascade, (leader/06, 008-form)
      runs *before* material_code, so a record with both signals will
      now resolve via leader+008. Update the test's expected outcome
      or pick test records where leader/06 is in the *empty* set so
      material_code remains the deciding layer.
- [ ] Re-run `scratchpad/rda-cascade/coverage_tally.py` on the same
      5k sample — Phase B alone should drop the residual from 566 to
      ≤ 50 records (the records whose leader/06 is unmapped or whose
      008 is malformed).

---

## Phase C — 300$a extent fallback + existing material/itype adapters

Estimated wall-time: half a day. Adds the last-resort textual layer
and adapts the existing material/itype tables to the PartialRda shape
so they slot into the cascade rather than being a separate path.

### C1. Layer 4-5 — material_code + itype adapters

Wrap the existing `MATERIAL_TO_RDA` and `ITYPE_TO_RDA` lookups into
PartialRda-returning layer functions:

```python
def from_material_code(ctx: RecordContext) -> PartialRda:
    rda = MATERIAL_TO_RDA.get(ctx.material_code or "")
    return _as_partial(rda)


def from_items_itype(ctx: RecordContext) -> PartialRda:
    rda = lookup_rda_for_items(ctx.items)
    return _as_partial(rda)
```

Slotted into `DEFAULT_LAYERS` *after* leader+008 so they only fire
on records the universal default couldn't resolve (extremely rare
on this corpus per Phase A; the layer exists as belt-and-braces).

### C2. Layer 6 — 300$a extent regex

Add `from_300_a_extent(ctx) -> PartialRda`. Last-resort textual
fallback for records whose leader/06 and 008-form are both
unmapped *and* whose material_code/itype don't help. Keys on
300$a tokens ("1 CD", "1 DVD-levy", "1 LP-levy", "1 äänikasetti",
"1 kirja", "1 kuvateos", "1 kartta", "1 esine", "1 nide").

17 % of current drops carry such a token; this layer is informational
in the regression-test residual but useful insurance on the full
800k corpus where the long-tail leader/06 distribution may differ.

### C3. Phase C acceptance

- [ ] `from_material_code`, `from_items_itype`, `from_300_a_extent`
      exist with unit tests.
- [ ] Each adapter layer is independently testable (separate
      function, separate test file or section).
- [ ] Re-run `coverage_tally.py`: residual ≤ 50 records (matches
      the plan goal — this is the plan-wide acceptance, not Phase C
      alone).
- [ ] Conflict-logging: when 007 and (leader/06, 008-form) disagree
      on carrier, the cascade composer writes a
      `scratchpad/rda-cascade/conflicts.jsonl` entry. Not surfaced
      as a failure — informational for cataloguers.

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
| (leader/06, 008-form) default mis-types non-default manifestations (e.g. e-book defaulted to print volume because 008/23 = ' ') | Medium | Pre-RDA Helmet records that ARE e-books / microform / braille typically code 008/23 — that's the whole point of the position. Records with 008/23 = ' ' and the manifestation is non-default are rare cataloguer-input errors. Provenance marker (Phase D) makes these correctable in bulk. |
| Sound recordings (leader/06 ∈ {i, j}) appear in the drop list after some future M2-gate change | Low | The gate currently exempts {c, d, i, j} from 33X-required. The lookup table includes entries for these to be ready, but they don't ship in the initial release because they don't appear in drop lists today. |
| Mis-mapping in 007/008 position tables (especially the dense sound-recording family) | Medium | Unit tests against LoC's canonical sample 007/008 per category. Initial 007 set covers s/v/c/t only; 008-form positions are encoded per leader/06 class per the LoC spec. |
| 007 / (leader/06, 008-form) disagree on the same record | Low-medium | Slot-wise priority resolves this — 007 wins on the carrier/media slots it fills, leader+008 fills the rest. Disagreements logged to `scratchpad/rda-cascade/conflicts.jsonl` for cataloguer review. |
| `$5 FI-HELME/synth-v<N>` is unexpected by downstream M2/M3 stages | Low | `$5` is a standard MARC subfield. Downstream stages already pass-through unknown subfields. Smoke-test on the 5k sample after Phase D. |
| Performance regression in per-row synth | Low-medium | The cascade reads more of each row but the data is already on the row (no extra DB / IO). Profile against the P-02 5k baseline; if regression > 10 % per-row, optimise the layer pipeline (cached `RecordContext` factory, early-exit when all slots fill). |
| Pre-RDA records with stale 008 form-of-item position get mis-tagged | Low | Accept — the cascade improves bulk recovery; rare migration-stale cases are noise on the order of cataloguer-correction throughput anyway. Provenance marker (Phase D) makes these correctable in bulk by re-cascading after the cataloguer flags the pattern. |
| Existing test `test_row_to_marcxml_bib_material_code_wins_over_items` semantics change (leader+008 now intercepts before material) | High | **Expected** — update the test in Phase B5. The new contract is "leader+008 wins over material_code wins over itype". Either pick test records where leader/06 is unmapped (preserving the material-code precedence) or rename the test to reflect the new layer order. |

## Rollback

Each phase ships behind a small commit; rollback is `git revert`
on the phase commit:

- **Phase D rollback**: removes the `$5` marker. No data loss; the
  next full export will re-emit synth 33X without the marker.
- **Phase C rollback**: removes the 300$a fallback + the adapter
  wrapping. Cascade falls back to layers 2-3 (leader+008 + 007).
  Drop count on the 5k sample is essentially unchanged — Phase B
  alone is projected to close most of the gap.
- **Phase B rollback**: removes the cascade scaffolding and the
  universal layer + 007 refinement. Cascade falls back to the
  existing `lookup_rda(material_code, item_dicts)` — the
  post-`46b0f8a` behaviour, ~566-record baseline.
- **Phase A rollback**: scratchpad-only; no production code change
  to revert. The plan revision in this commit, if reverted, also
  reverts the layer-set decision — make sure to re-do the
  coverage analysis before re-deciding.

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
- **The 008-form position table** covers leader/06 ∈ {a, c, d, e, f,
  g, i, j, k, m, o, r, t}. Are any others present in production? Run
  the leader/06 histogram against the full 800k corpus before Phase B
  ships; if any leader/06 outside this set appears with non-trivial
  count, add it to the table or document the omission.
- **Counterpoint — is 566-down-to-50 enough to justify 2-3 days?**
  Same calculus as the proposal stage: the drop rate matters mainly
  insofar as cataloguers care about the missing records being in
  Skosmos. If Helmet is fine with the 11 % residual being re-coded
  over time, P-08 stays in `in-progress/` and the cataloguers proceed.
- **Cross-product with P-07 (856-as-Item)**: P-07 also touches M2
  routing. Phase D's `$5` marker is independent (additive subfield
  on the synth 33X datafield, nothing P-07 touches), so the two
  plans can ship in either order.

## Cross-references

- [`docs/proposals/prop-08-richer-rda-33x-synthesis.md`](../../proposals/prop-08-richer-rda-33x-synthesis.md)
  — graduated source proposal; planning-graduated stub.
- [`src/marcxml_export_pipeline/sierra/itype_to_rda.py`](../../../src/marcxml_export_pipeline/sierra/itype_to_rda.py)
  — `ITYPE_TO_RDA` and `MATERIAL_TO_RDA` preserved as cascade
  adapters (layers 4-5). `lookup_rda` stays; the call site in
  `marcxml.py` is replaced by the slot-wise composer in Phase B4.
- [`src/marcxml_export_pipeline/sierra/marcxml.py`](../../../src/marcxml_export_pipeline/sierra/marcxml.py)
  `build_marcxml_for_row` — call site replaced in Phase B4.
- [`src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql`](../../../src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql)
  — already streams the signals P-08 needs. No SQL changes required.
- [`src/bffi_pipeline/validation/marcxml.py`](../../../src/bffi_pipeline/validation/marcxml.py)
  `marcxml-content-minimum` gate — gate logic unchanged; P-08 lifts
  records *over* the gate rather than weakening it.
- [`docs/external-dependencies.md`](../../external-dependencies.md)
  — updated in Phase D2 with cataloguer-facing notes.
- [`scratchpad/rda-cascade/`](../../../scratchpad/rda-cascade/) —
  Phase A coverage analysis (gitignored). `coverage_tally.py` can
  re-run after each phase as a regression check.
- LoC MARC 21 Bibliographic spec, § 008 (Form of item positions per
  leader/06 class) and § 007 (per-category subfield positions) —
  source of truth for the position tables. Cite specific section
  numbers in the layer docstrings.

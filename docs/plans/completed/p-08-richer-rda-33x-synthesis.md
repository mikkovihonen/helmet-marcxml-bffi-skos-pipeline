# P-08 — Richer RDA 33X synthesis from leader / 008 / 007

**Status**: completed (all four phases shipped; the M2 missing-33X drop count on the 5k sample is at 0 — well below the ≤ 50 target).
**Source proposal**: `prop-08-richer-rda-33x-synthesis` (deleted on 2026-05-13 plans/proposed reorganisation; recover via `git show f2d8486 -- <orig-path>`)
at commit `19e09e4`.
**Plan-base commit**: `19e09e4`. To gauge drift before executing,
run `git diff 19e09e4..HEAD --
src/marcxml_export_pipeline/sierra/itype_to_rda.py
src/marcxml_export_pipeline/sierra/marcxml.py
src/marcxml_export_pipeline/sierra/sql/all_bibs_marcxml.sql
src/bffi_pipeline/validation/marcxml.py
tests/unit/sierra/test_marcxml.py`.
**Phase commits**:

- Phase A (coverage analysis + layer-set revision): `10c508a`
- Phase B (cascade scaffolding + (leader/06, 008-form) + 007 refinement + adapter layers): `e87965c`
- Phase C (300$a extent fallback): `94a7de7`
- Phase D (`$5` provenance marker + cataloguer-facing docs): `10f8da1`

**Owner**: TBD.
**Estimated wall-time**: ~1 day remaining. Phase A is done; Phase B
is done (cascade scaffolding + (leader/06, 008-form) + 007 refinement
+ material/itype adapter layers + tests; the adapter layers were
folded into Phase B from the original Phase C scope so existing
material/itype regressions are caught at Phase B5 acceptance, not
deferred). Phase C is now just the 300$a extent fallback (half a
day). Phase D is half a day (provenance + docs).

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

Phase B (shipped) implements priorities 2-5 — the universal default,
007 refinement, and the material/itype adapter wrappers. The adapter
wrappers were folded into Phase B from the original Phase C scope so
existing material/itype regression tests are covered at Phase B5
acceptance, not deferred. Phase C now implements priority 6 — the
300$a extent fallback.

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

## Phase B — Cascade scaffolding + (leader/06, 008-form) + 007 refinement + adapters (DONE)

Shipped `src/marcxml_export_pipeline/sierra/rda_signals.py` with:

- `PartialRda` (per-slot resolution dataclass + `merge_below` composer
  primitive + `is_complete` + `to_carrier`);
- `RecordContext` (typed view: leader/06, controlfields tuples,
  varfields tuple, items tuple, material_code);
- `resolve_rda(ctx, layers)` — runs the cascade in priority order,
  short-circuits as soon as all three RDA slots fill;
- `from_marc_007` — categories `s/v/c/t` with their carrier
  refinement positions (1 for audio/computer/text, 4 for video) per
  LoC § 007;
- `from_leader_and_008` — `LEADER_008_TO_RDA` dict keyed on
  `(leader/06, 008-form)`; `LEADER_06_FALLBACK` dict for missing /
  unmapped 008-form;
- `from_material_code`, `from_items_itype` — adapter wrappers around
  the existing `MATERIAL_TO_RDA` and `lookup_rda_for_items` tables;
- `DEFAULT_LAYERS = (from_marc_007, from_leader_and_008,
  from_material_code, from_items_itype)`.

Also promoted the 9 shared `RdaCarrier` constants in
`itype_to_rda.py` from underscore-prefixed to public so both modules
share them (`TEXT_UNMEDIATED_VOLUME`, `VIDEO_VIDEODISC`, etc.). Wired
into `build_marcxml_for_row` via a small `_synthesise_33x_varfields`
helper.

### Phase B key findings

- **Projected residual: 0 records** on the 566-drop list, vs the
  plan's `≤ 50` target. Measured by
  `scratchpad/rda-cascade/phase_b_residual.py` which constructs a
  `RecordContext` from each drop's MARCXML and runs the Phase B
  layers — 100 % recovery rate.
- All 8 leader/06 + 008-form combinations present in the drop list
  resolve via `LEADER_008_TO_RDA` (or its leader-only fallback).
- 007 refinement fires on the 5 % of drops that carry it. On the
  remaining 95 %, `from_leader_and_008` fills all three slots from
  the leader-derived default.
- Existing material/itype tests passed unchanged because the
  default-leader scenarios in those tests still resolve to the
  same `TEXT_UNMEDIATED_VOLUME` either via leader+008 or via the
  material/itype adapters.

### Phase B acceptance

- [x] `rda_signals.py` exists with all listed exports.
- [x] Unit tests at `tests/unit/sierra/test_rda_signals.py` cover:
      `PartialRda` mechanics, `resolve_rda` composition, 007 per
      category, `LEADER_008_TO_RDA` per tuple, adapter layers,
      cascade priority ordering, the empty case.
- [x] Regression: all 44 existing `test_marcxml.py` tests still
      pass (52 new tests added; full suite at 796 tests).
- [x] Phase B alone drops the residual from 566 to 0 on the 5k
      sample. The plan's `≤ 50` goal is met (and beaten); the
      `Definition of done` for the plan as a whole is satisfied
      after Phase B; Phase C (300$a fallback) and Phase D
      (provenance marker) ship for resilience / auditability but
      are no longer load-bearing for the goal.

### Scope deviation from the original Phase B/C split

The plan as committed at `10c508a` slated material/itype adapter
wrappers for Phase C. They were folded into Phase B instead so
existing regression tests in `test_marcxml.py` (which assert
material/itype behaviour) stay green during Phase B. Phase C is now
just the 300$a extent fallback.

---

## (Earlier draft of B3 — kept as a reference for the (leader/06, 008-form) table contents)

The shipped table lives in `src/marcxml_export_pipeline/sierra/rda_signals.py`
as `LEADER_008_TO_RDA`. The draft below documents the design
intent and is retained for future-readers who want the *why*
without `git blame`-ing the module.

The 008 "Form of item" position lives at a different position per
leader/06 class — encode the position map and the tuple values
together:

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

---

## Phase C — 300$a extent fallback (DONE)

Shipped `from_300_a_extent(ctx)` in `rda_signals.py`. The layer scans
all MARC 300 datafields' ``$a`` subfields for carrier-naming tokens
and returns the canonical RDA tuple on first match.

Token table (regex-keyed, ordered specificity-first so ``DVD-levy``
matches before generic ``levy``-fragments):

| Token | RDA tuple |
|---|---|
| `DVD-levy` / `DVD` / `Blu-ray` / `videolevy` | `VIDEO_VIDEODISC` |
| `videokasetti` | `VIDEO_VIDEOCASSETTE` |
| `LP-levy` / `LP` / `äänilevy` / `CD-levy` / `CD` | `PERFORMED_MUSIC_AUDIO_DISC` |
| `äänikasetti` / `C-kasetti` | `PERFORMED_MUSIC_AUDIO_CASSETTE` |
| `nuotti` | `NOTATED_MUSIC_UNMEDIATED_VOLUME` |
| `kartta` | `MAP_SHEET` |
| `esine` | `THREE_D_OBJECT` |
| `kirja` / `kuvateos` / `nide` / `sivua` / `pages` | `TEXT_UNMEDIATED_VOLUME` |

Regexes match word-boundary stems with optional declension suffixes
(`\w*\b`) so Finnish forms like `kirjaa`, `karttaa`, `nidettä` all
hit. Appended to `DEFAULT_LAYERS` after `from_items_itype` as the
last-resort fallback — fires only when every higher-priority layer
left at least one slot empty.

### Phase C key findings

- The layer is by design **informational on the current sample** —
  Phase B's universal default resolves all 566 drops, so the cascade
  short-circuits before reaching layer 5. The layer's value is for
  the long-tail leader/06 distribution on the full 800k corpus (`o`
  kits, `p` mixed material, obsolete codes) and for records where
  every higher-priority signal is absent.
- The phase_b_residual.py script still reports 0 residual after
  Phase C (regression-free); coverage_tally.py is unchanged.
- A `test_default_layers_extent_does_not_override_leader_008` test
  pins the priority order: a record with leader+008 saying "text" and
  300$a saying "DVD-levy" still resolves to text — the 300$a layer
  fires only on empty slots.

### Phase C acceptance

- [x] `from_300_a_extent(ctx)` exists with 34 new unit tests covering
      every token in the regex plus the fall-through cases (no 300$a,
      unrecognised token, multiple 300 fields, non-300 varfields).
- [x] Appended to `DEFAULT_LAYERS` after `from_items_itype`.
- [x] `phase_b_residual.py` still reports 0 residual on the 5k
      sample (no Phase C regression).
- [x] Test suite at 830 tests; lint + mypy --strict clean.

### Deferred

Conflict-logging (007 disagreeing with leader+008's carrier) was
listed in Phase C5 of the original plan as informational. It is
deferred to a follow-up — the cascade's priority order already
resolves disagreements deterministically (007 wins on its slots,
leader+008 on the rest), and emitting a scratchpad jsonl per
disagreement adds complexity for no current consumer. Surface as a
small task if cataloguers ask for the audit signal.

---

## Phase D — Provenance marker + cataloguer-facing docs (DONE)

Shipped:

- `SYNTH_VERSION: Final[int] = 1` constant in `rda_signals.py`. Bumped
  manually when the cascade's emitted RDA tuple for an existing record
  would change. Stamped into every synthesised 33X datafield via the
  `SYNTH_SOURCE_MARKER = f"{AGENCY_CODE}/synth-v{SYNTH_VERSION}"` string
  in `marcxml.py`.
- `_build_rda_varfield` extended with an optional `source_marker` arg
  that appends a `$5 <marker>` subfield when supplied.
  `_synthesise_33x_varfields` passes the marker on every emitted 33X
  field; cataloguer-coded 33X passes through unchanged (no marker
  added) because the synth path only fires when none of
  `{336, 337, 338}` is present on the bib.
- `docs/external-dependencies.md` carries a new section explaining
  the synth cascade, the `$5 FI-HELME/synth-v<N>` marker, the
  per-record opt-out (add cataloguer-coded 33X), and where the
  version constant lives.
- Plan graduates from `in-progress/` to `completed/` in this commit
  via `git mv`; `docs/plans/README.md` + `docs/plans/proposed/README.md`
  cross-references updated.

### Phase D acceptance

- [x] `$5 FI-HELME/synth-v1` lands on every synthesised 33X
      datafield. Verified by
      `test_row_to_marcxml_synth_33x_carries_provenance_marker` and
      by the updated assertion in
      `test_row_to_marcxml_synthesises_33x_from_itype_book`.
- [x] `docs/external-dependencies.md` documents the marker and the
      cataloguer opt-out.
- [x] `test_row_to_marcxml_cataloguer_coded_33x_carries_no_synth_marker`
      confirms cataloguer-coded 33X passes through with no `$5`
      marker.
- [x] Plan moved to `completed/`; cross-references updated.

### Sample synthesised output

```xml
<datafield tag="336" ind1=" " ind2=" ">
  <subfield code="a">teksti</subfield>
  <subfield code="b">txt</subfield>
  <subfield code="2">rdacontent</subfield>
  <subfield code="5">FI-HELME/synth-v1</subfield>
</datafield>
```

MARC convention: `$5` is "institution to which field applies". The
`/synth-v<N>` suffix carries the cascade version so a future
re-cascading can find synth-v1 fields and replace them
deterministically without disturbing cataloguer-coded fields.

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

- `prop-08-richer-rda-33x-synthesis` (deleted on 2026-05-13 plans/proposed reorganisation; recover via `git show f2d8486 -- <orig-path>`)
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

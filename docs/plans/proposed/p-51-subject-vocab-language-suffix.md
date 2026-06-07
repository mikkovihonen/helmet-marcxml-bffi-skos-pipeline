# P-51 — MARC 6XX `$2` vocabulary language suffix (`yso/fin`)

**Status**: proposed. **Deferred pending data.** Decide-when-shipping after the post-corpus-concat-fix pipeline run shows actual `$2` diff residue volume.

**Scope**: round-trip emission policy for the language part of MARC 6XX / 655 `$2` subfields (`yso/fin`, `yso/swe`, `kauno/fin`, etc.). Implementation is bounded (~15 lines + table + tests) regardless of which option ships; the substantive question is **which policy**.

**Proposal-base commit**: HEAD of the corpus-concat fix (see commit `<unfilled>` from the same session). Without that fix, the diff noise from this issue is hidden behind a more serious namespace bug; with it, the real `$2`-suffix residue becomes the dominant signal.

## Motivation

The MARC 6XX / 655 `$2` subfield names the source vocabulary for the subject heading. Helmet has two cataloguing conventions in the wild:

- **Legacy** (pre-Finto-modernisation): `$2 yso/fin`, `$2 yso/swe`, `$2 kauno/fin` — vocab code plus a 3-letter language indicating which language's prefLabel was used.
- **Modern** (post-Finto / RDA): `$2 yso`, `$2 kauno` — bare vocab code; the cataloguer's `$a` literal carries the language implicitly.

The round-trip converter currently emits the modern bare form (commits `add2d5e` and `5255830` debated this and settled on bare-output). Records that were catalogued under the legacy convention surface as `changed` diff rows on every 6XX with a `/fin`/`/swe` suffix — they're not data errors, they're convention drift.

### What we can't do

A probe (`/tmp/probe_dollar2.py` in the same session, ~5 min, summarised below) established that `$2 yso/fin` and `$2 yso` produce **byte-identical BIBFRAME**:

| Source MARC | bf:source URI | bf:source bnode's bf:code | bflc:marcKey on subject |
|---|---|---|---|
| `$2 yso/fin` | `<…/subjectSchemes/yso>` | `"yso"` | (not emitted for $0-keyed) |
| `$2 yso/swe` | `<…/subjectSchemes/yso>` | `"yso"` | (not emitted) |
| `$2 yso` | `<…/subjectSchemes/yso>` | `"yso"` | (not emitted) |

The `/fin` / `/swe` suffix is **silently dropped** at marc2bibframe2's XSL layer; no surviving triple carries the cataloguer's exact `$2` value. Verbatim preservation is impossible at our pipeline boundary unless we either (a) modify the marc2bibframe2 submodule (prohibited per `CLAUDE.md`) or (b) read the source MARCXML at M2-post time and synthesise an extension predicate (substantial scope; same compatibility caveats as the LDR/19 deferred work).

### What we CAN do — synthesis from cross-record signals

Three signals remain available downstream of M2:

1. **The `$a` label's language tag** — when the converter picks an `rdfs:label` / `skos:prefLabel` literal, it carries a BCP-47 language tag (`@fi`, `@sv`, …). Already used in the `lang_pref=("fi", "sv", "en")` preference walk in `_loc_label`. Mapping to MARC 3-letter codes (`fi` → `fin`, `sv` → `swe`, `en` → `eng`) is a one-table reverse of `_LANG_3_TO_2`.
2. **The record's primary language** — M3's title-language cascade detects this; available via `_primary_record_language(source)` from `m3/post_process.py`.
3. **Per-vocabulary convention** — historical Helmet practice attached `/<lang3>` to multilingual vocabularies (YSO, KAUNO, KAUNOKKI, MUSA, CILLA) and bare names to monolingual ones (Allärs is Swedish-only — no `/swe` suffix needed; YKL is Finnish-only — no `/fin`).

## Options

### Option A — Always-suffix from `$a` label language

```python
label, lang = self._first_label_with_lang(uri)
suffix = _LANG_2_TO_3.get(lang)
return f"{vocab}/{suffix}" if suffix else vocab
```

- ✓ Simplest, ~10 lines.
- ✓ Matches legacy-Helmet pattern.
- ✗ Indiscriminate: records originally typed `$2 yso` (modern) now reconstruct as `yso/fin`. New `changed` diff rows on every modern record — same noise volume, different rows.
- ✗ Reintroduces a convention we agreed was deprecated.

### Option B — Per-record primary-language guard

```python
record_lang = self._primary_record_language()
label, lang = self._first_label_with_lang(uri)
if record_lang and lang == record_lang:
    return f"{vocab}/{_LANG_2_TO_3[lang]}"
return vocab
```

- ✓ Less false-positive than A: suffix only when the label's language matches the record's primary language.
- ✓ Reuses existing M3 detection.
- ✗ Still indiscriminate at cataloguer level — modern records get suffixed.
- ✗ Awkward on cross-language subjects (a Finnish-language book carrying a Swedish geographic subject).

### Option C — Vocab-aware allowlist

```python
_VOCAB_USES_LANG_SUFFIX = {"yso", "kauno", "kaunokki", "musa", "cilla"}
if vocab in _VOCAB_USES_LANG_SUFFIX:
    label, lang = self._first_label_with_lang(uri)
    if lang:
        return f"{vocab}/{_LANG_2_TO_3[lang]}"
return vocab
```

- ✓ Suffix only multilingual vocabularies. Allärs (Swedish-only), YKL (Finnish-only), SLM (Finnish) stay bare — no spurious `allars/swe`.
- ✓ Respects the underlying vocabulary structure rather than imposing a uniform policy.
- ✗ Curated allowlist; needs an entry when Finto publishes a new vocab.
- ✗ Same false-positive on in-scope vocabs as A/B for modern records.

### Option D — Operator-chosen policy (config knob)

```ini
# config/bffi.cfg
[round-trip]
subject_language_suffix = modern  # legacy | modern | per-record
```

- ✓ Defers the convention choice to Helmet rather than the pipeline.
- ✓ Reversible without code change.
- ✗ Another configuration knob; needs documentation.
- ✗ Risk of operator/source convention mismatch — same noise per setting.

### Option E — Don't synthesise (status quo)

- ✓ Honest: we don't fabricate data we can't recover.
- ✓ Diff noise on legacy records is operationally a "this record was catalogued under the old convention" signal — useful information.
- ✗ Diff noise volume could be significant if Helmet's legacy convention dominates the corpus.

## Comparison

| Option | False positive (modern records) | False negative (legacy records) | Impl size | Mainenance |
|---|---|---|---|---|
| A — always | Many | None | ~10 LoC | None |
| B — record-language guard | Many | Few (cross-language subjects) | ~20 LoC | None |
| C — vocab allowlist | Many in-vocab | None in-vocab | ~15 LoC + table | Allowlist drift |
| D — config knob | Operator's call | Operator's call | ~30 LoC + config | Documentation |
| E — status quo | None | All legacy records | 0 | None |

## Why deferred

The decision depends on **how much of the diff residue is `$2` language-suffix noise after the corpus-concat fix lands**. The previous diff data was contaminated by the more serious bflc-vs-bffi-prov namespace bug (commit `<unfilled>`); that bug's repair pushes many `changed` rows back to `identical`, and we can't tell whether `$2` suffix noise is a top-10 issue or a tail-10 issue until we see the cleaner residue.

**Activation criterion**: after the next full-pipeline run on the corpus-concat-fix HEAD, if `$2`-language-suffix `changed` rows exceed (say) 5% of all 6XX/655 rows on the 500-record sample, graduate this proposal and pick a policy.

If `$2` noise stays under 5%, leave at Option E (status quo) — the diff information value of "this record used legacy convention" outweighs the noise.

## Recommended option (if/when activated)

**Option C — vocab-aware allowlist.** It:

- Respects the underlying vocabulary structure (no `allars/swe`).
- Matches legacy Helmet for multilingual vocabs where the suffix actually carries information.
- Doesn't pretend to know the cataloguer's modern-vs-legacy choice — it just synthesises the convention that **always applied** for the multilingual vocab.

Implementation outline:

- Add `_LANG_2_TO_3` inverse map alongside `_LANG_3_TO_2` in `m3/post_process.py` (or `marc_roundtrip/converter.py`).
- Add `_VOCAB_USES_LANG_SUFFIX` set to `marc_roundtrip/converter.py`.
- Extend `_first_source` / `_first_label` to return the label's language tag.
- Update `_first_source`'s callers to pass language through the `$2` synthesis.
- Tests: 4 cases (yso/fi, yso/sv, allars, kauno/fi); 2 negative cases (record with no $a language, English $a).

## Out of scope

- Modifying marc2bibframe2 to preserve the source `$2` literal. Prohibited per `CLAUDE.md`.
- Synthesising the suffix from MARC 008 pos 35-37 (resource language). Tested for correlation in a sample — not robust enough; cataloguers regularly use Finnish-labeled YSO terms on Swedish-language books and vice versa.
- A separate `language-reconciled` diff status (task #149) that would treat language-suffix differences as "equivalent" without re-emitting them. Distinct concern; could complement Option E if we want diff noise reduction without synthesis.

## Suggested next step

Watch the next pipeline run's `marc-roundtrip/summary.json` `changed` count. If the count stays high AND a manual spot-check of 6XX `changed` rows shows `$2 yso` vs `$2 yso/fin` as the dominant pattern, graduate this to `docs/plans/backlog/` and ship Option C.

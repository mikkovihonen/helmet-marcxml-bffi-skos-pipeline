# P-15 — Preserve cataloguer-supplied authority URIs through M3, so bilingual subjects don't re-reconcile to different URIs

**Status**: completed (2026-05-13).
**Source proposal**: `prop-15-bilingual-subject-reconciliation` (deleted on graduation; recover via `git show 6b62eea~3:docs/plans/proposed/prop-15-bilingual-subject-reconciliation.md`).
**Plan-base commit**: `1082d32`. To gauge drift before re-executing or backporting, run
`git diff 1082d32..HEAD --
sparql/
src/bffi_pipeline/stages/bf_to_bffi.py`.
**Phase commits**:

- Phase A (M3 SPARQL CONSTRUCT preserves `madsrdf:isIdentifiedByAuthority` + audit re-run): `02006b4` (code, 2026-05-13). One-clause change in `sparql/bf_to_bffi_work.rq` plus two unit tests pinning the contract. Verified against `scratchpad/data-cataloguer-audit-2026-05-13-v2/`: `b26322791`'s `Italia` / `Italien` now both resolve to `yso:p105111` via M3 directly; M9 has zero reconciliation activities for them (entities arrive pre-bound). M9 wall on the 19-record audit dropped 57 s → 48 s as a side benefit.

**Owner**: shipped this session.
**Estimated wall-time**: ~1 day. Actual: ~30 min (SPARQL change + tests + audit re-run + verification).

## Goal achieved

The 2026-05-13 cataloguer-audit surfaced `b26322791` carrying both `Italia` and `Italien` as separate `bffi:subject` URIs (`yso:p105111` and `allars:Y30493`) despite the source MARCXML tagging both with the same `$0 yso:p105111`. The fix preserves the `madsrdf:isIdentifiedByAuthority` cross-link that marc2bibframe2 emits on `bf:Place` / `bf:Person` / `bf:Organization` / `bf:Meeting` subjects, so the cataloguer-supplied authority URI propagates through M3 to canonical without being lost.

Post-fix verification: `b26322791`'s canonical Work has one `bffi:subject <yso:p105111>` triple (the Italian + Italian forms collapse to the same RDF triple after sharing the same URI), no `allars:Y30493`, no `urn:...raw/...#Place651-XX` URIs.

## Definition of done

- [x] M3 SPARQL CONSTRUCT change preserves `madsrdf:isIdentifiedByAuthority` URIs for `bf:Place` (and by the same code path any other `bf:` entity carrying the predicate).
- [x] Unit tests pin both branches: (a) entity with cross-link → authority URI lands in `bffi:subject`; (b) entity without cross-link → bf:subject URI lands (no regression for `bf:Topic` case where the YSO URI is already the entity's `rdf:about`).
- [x] `b26322791` audit re-run: the canonical Work has exactly one `bffi:subject <yso:p105111>` triple and zero `allars`/raw URIs.
- [x] M9 provenance on the re-run shows zero reconciliation activities for `b26322791`'s Italia / Italien literals.
- [x] No regression on the wider 19-record audit set. M9 outcome rates match or improve.

## What this plan does NOT do (deferred)

- `bf:Agent` contributors use a different cross-link shape (`bf:identifiedBy → bf:Identifier { rdf:value; bf:source [bf:code "FI-ASTERI-N"] }`) and need URI reconstruction rather than a direct COALESCE. Not addressed here; tracked as a follow-up if the wider P-14 audit shows it's worth doing. The audit signal exists — 5/5 Category 1 KANTO authors going through tier-2 LLM rather than tier-0 — but the fix is structurally different.
- The `skos:exactMatch` allars → yso crosswalk originally proposed (in the rejected first cut of prop-15) is no longer needed for the b26322791 case. It remains a smaller residual case for records that carry a Swedish-only `$0 allars:...` URI; not pursued.

# In progress

Plans where at least one phase has shipped but the plan's
"Definition of done" hasn't been met yet. The plan's `Phase
commits` field should carry concrete commit hashes for the shipped
phases and `<unfilled>` for the ones still ahead.

When the final phase commits and the plan's definition of done is
green, `git mv` the plan into [`../completed/`](../completed/) in
the same commit.

If the plan is dropped before completion, `git mv` it to
[`../abandoned/`](../abandoned/) and add a short
`Abandonment reason` section near the top.

## Current in-progress plans

- [`p-35-m3-cascade-follow-ups.md`](p-35-m3-cascade-follow-ups.md)
  — Renumbered from P-05 + graduated from `backlog/` 2026-05-14 to
  clear a number collision with the now-abandoned
  `proposed/p-05-anonymous-work-canonicalisation.md`. Phase F1
  (M8 propagates non-primary `bffi:Contribution` blocks onto
  canonical Expressions) was shipped pre-renumber at `464247e`
  (initial propagation) + `b56d9c1` (role through-propagation);
  the `<unfilled>` Phase-commits field that the rename caught
  was pure documentation rot. F2 (transliteration sidecar +
  M9 binding) and F3 (M9 walks non-primary canonical
  contributions for KANTO reconciliation) still backlog; F3
  pre-gated on `gold/contrib.jsonl` reaching 30-50
  cataloguer-vetted cases (tracked under P-06).
- [`p-41-minimum-bibliographic-synthesis-and-export-report.md`](p-41-minimum-bibliographic-synthesis-and-export-report.md)
  — Graduated from `proposed/` on 2026-06-02; all three phases
  scheduled in the same implementation session. **Motivation:**
  M2's `marcxml-content-minimum` validator drops records missing
  1XX/7XX (creator) — 91 / 5 000 (1.82 %) on the 2026-05-14 5 k
  sample, ~14 500 records projected to the 800 k corpus.
  **Phase A** specs the "minimum exportable" contract + adds the
  `bffi-prov:Synthesis` Activity class with four `synthetic*`
  predicates. **Phase B** adds a tiered salvage layer at
  `marcxml_repair.py`: B1 = 245$c parse (regex + LLM cascade with
  verbatim-substring constraint + `synth-cache.sqlite`); B2 =
  publisher-as-corporate-creator (code lands behind a feature flag
  default-off pending cataloguer leader/06 sign-off); B3 =
  anonymous-by-convention sentinel agent
  `http://urn.fi/URN:NBN:fi:bib:agent:unknown` with the
  `bffi:syntheticSentinel` flag wired into M5/M6/M8/M9 exclude rules.
  **Phase C** adds the per-run `export-synthesis-<run_uuid>.tsv`
  writer + the retrospective `bffi-pipeline export-synthesis-report
  --run <uuid>` CLI that re-derives the TSV from `data/provenance.ttl`.
  **P-39 integration:** Phase B.6 emits the `bffi:syntheticSentinel`
  flag that P-39's M9 walker amendment skips on — single-predicate
  contract between the two plans.

P-32 (Run lifecycle management) graduated to
[`../completed/`](../completed/) at `fdae706` (Phase D — final phase).

P-34 (M8 mint for anonymous-main-entry records) graduated to
[`../completed/`](../completed/) at `c2d5b2b` after Phase B
shipped (synthetic anchor on title + content-type + language,
recovering the final 45 mint-failure records). Phase C
(mint-key refactor) was removed from the DoD — corpus coverage
at 99.96 % made it unnecessary.

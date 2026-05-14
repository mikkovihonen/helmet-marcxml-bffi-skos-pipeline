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

P-32 (Run lifecycle management) graduated to
[`../completed/`](../completed/) at `fdae706` (Phase D — final phase).

P-34 (M8 mint for anonymous-main-entry records) graduated to
[`../completed/`](../completed/) at `c2d5b2b` after Phase B
shipped (synthetic anchor on title + content-type + language,
recovering the final 45 mint-failure records). Phase C
(mint-key refactor) was removed from the DoD — corpus coverage
at 99.96 % made it unnecessary.

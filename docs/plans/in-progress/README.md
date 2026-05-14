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

- [`p-34-m8-mint-anonymous-main-entry-works.md`](p-34-m8-mint-anonymous-main-entry-works.md)
  — graduated from `proposed/` 2026-05-14. Phase A (editor-anchored
  fallback for anonymous-main-entry records: walks `Work →
  bffi:contribution` + `Work → hasExpression → bffi:contribution`
  when `bffi:PrimaryContribution` is absent, picks the lex-min
  non-translator agent URI, emits `bffi-prov:mintAnchor` on the
  canonical Work to distinguish editor-anchored from
  primary-author-anchored Works; 4 unit tests; translator-role
  blocklist on LoC `relators/trl` + free-text labels in
  fi/sv/en/de) shipped at `<unfilled>`. Bench result: 662 of 707
  previously-dropped records recovered into the canonical graph
  (98.4% coverage on the 2026-05-14 helmet-5k sample, up from
  84.9%). Phase B (title-only mint + cataloguer rule table for the
  residual 0.9%) backlog; gated on cataloguer-side ask. Phase C
  (mint-key refactor) deferred indefinitely.

P-32 (Run lifecycle management) graduated to
[`../completed/`](../completed/) at `fdae706` (Phase D — final phase).

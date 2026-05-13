# Archived

Documents in this folder are kept for **reference and historical
audit only**. They captured the project's state at a particular
moment, are no longer the source of truth for ongoing work, and
should not be edited except to fix obvious typos or to add an
explanatory pointer to the document that superseded them.

If you find yourself reaching for a document here to decide what to
do next, that's a signal the supersede pointer is missing — fix it
when you spot the gap.

Path references from source code or live docs may still point in
here (e.g. for milestone-section context); those are fine. Links
into archived material from a live plan / proposal / spec are also
fine as long as the live document does not depend on the archived
one being mutable.

## Current contents

- [`BUILD_PLAN.md`](BUILD_PLAN.md) — the original milestone-ordered
  build plan (M0–M13). Superseded by the individual plan documents
  under [`docs/plans/`](../plans/) which carry the sequenced
  execution detail. The milestone checkboxes remain a useful
  historical record of what shipped when, but the live work tracking
  has moved to per-plan phase commits.
- [`marcxml-to-bffi-skosmos-pipeline.md`](marcxml-to-bffi-skosmos-pipeline.md)
  — the original end-to-end technical specification. Section-level
  back-references from older commits, plans, and source comments still
  point here, so the document is preserved for navigability. Live
  successors: [`CLAUDE.md`](../../CLAUDE.md) for committed identifiers
  and `bffi-prov` enums; [`../tech-stack.md`](../tech-stack.md) for the
  toolchain; per-plan documents under [`../plans/`](../plans/) for the
  execution detail; [`../plans/proposed/`](../plans/proposed/) for forward-looking
  design changes.
- [`local-inference.md`](local-inference.md) — the previous
  Ollama-centric and `mlx_lm` source-clone draft of the inference
  runbook. Superseded by [`../local-inference.md`](../local-inference.md),
  which is mlx-lm-first and PyPI-installable. Kept for the historical
  decision trail between Ollama → mlx_lm → vllm-mlx → mlx-lm.
- [`cataloguer-asks-fi.md`](cataloguer-asks-fi.md) — Finnish-language
  cataloguer-facing copy of the requests in
  [`../external-dependencies.md`](../external-dependencies.md). Retained
  as a snapshot; regenerate from the live English document if a fresh
  Finnish version is needed for re-distribution.

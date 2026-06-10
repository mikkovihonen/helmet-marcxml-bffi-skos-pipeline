"""Pillar 3 — BIBFRAME → BFFI canonical Turtle.

Forward conversion via SPARQL CONSTRUCT (or equivalent RDF processing).
Emits BFFI-only canonical Turtle — zero ``bf:*`` URIs in the output, per
the p-56 hard-cut transition. Every routing decision follows
``docs/bf_to_bffi_mapping.md``.

Stage label for observability sidecar events: ``bibframe2bffi``.
"""

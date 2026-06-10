"""Pillar 4 — BFFI graph → reconstructed MARCXML (reverse direction).

Reads BFFI predicates only. Cardinal rule: the reverse converter MUST
NOT consult ``bffi-prov:`` (pipeline-internal provenance) for
bibliographic-content decisions. Pipeline-internal data is fair for UI /
pairing machinery, never for emit content.

Stage label for observability sidecar events: ``bffi2marc``.
"""

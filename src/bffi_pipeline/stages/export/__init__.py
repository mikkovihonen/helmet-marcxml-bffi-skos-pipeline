"""Pillar 1 — Sierra (Postgres replica) → MARCXML export.

Wraps :mod:`marcxml_export_pipeline.sierra.marcxml` to produce per-record
MARCXML files matching the existing Helmet corpus layout.

Stage label for observability sidecar events: ``export``.
"""

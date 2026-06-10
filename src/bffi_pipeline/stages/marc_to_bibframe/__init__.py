"""Pillar 2 — MARCXML → BIBFRAME via the LoC marc2bibframe2 XSLT.

Thin driver around the vendored ``third_party/marc2bibframe2/`` XSLT.
Behaviour unchanged from the upstream LoC project; we only manage the
inputs/outputs and emit observability events.

Stage label for observability sidecar events: ``marc2bibframe``.
"""

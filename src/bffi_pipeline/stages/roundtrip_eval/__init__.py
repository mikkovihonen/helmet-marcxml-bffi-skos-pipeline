"""Pillar 5 — Round-trip eval harness.

Diffs source MARCXML against reconstructed MARCXML (from pillar 4); emits
per-record diff status (``identical`` / ``changed`` / ``lost`` /
``tag-changed`` / ``marckey-bypass``), aggregate counts for the dashboard,
and a cataloguer-review HTML covering the residue.

Stage label for observability sidecar events: ``roundtrip_eval``.
"""

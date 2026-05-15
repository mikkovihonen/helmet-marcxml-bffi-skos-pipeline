"""M5 similarity-band classification.

Cosine similarities returned by the FAISS HNSW search land in one of
three bands; pairs in the *escalate* band are the only ones the M6
LLM judge ever sees. Thresholds are tightened from spec § 6's
frontier-API defaults because the M5 Max local-judge pass is
throughput-bound.

The ``_bucket_label`` helper is used by :mod:`build`'s embed-stats
histogram and lives here alongside the band logic — both are
similarity-binning concerns.

P-38 Phase D: extracted from m5/runner.py to keep the runner focused
on the operator-entry surface. No logic change — moves only.
"""

from __future__ import annotations

from typing import Final, Literal

Band = Literal["auto-merge", "escalate", "reject"]

#: Tightened from the spec's frontier-API defaults because the M5 Max
#: LLM judge is throughput-bound; see
#: docs/archived/marcxml-to-bffi-skosmos-pipeline.md § 6.
BAND_AUTO_MERGE: Final[float] = 0.90
BAND_REJECT: Final[float] = 0.78


def classify_band(similarity: float) -> Band:
    """Sort a cosine similarity into the three-band space.

    - ``>= BAND_AUTO_MERGE`` → ``"auto-merge"``
    - ``BAND_REJECT < s < BAND_AUTO_MERGE`` → ``"escalate"``
    - ``<= BAND_REJECT`` → ``"reject"``
    """
    if similarity >= BAND_AUTO_MERGE:
        return "auto-merge"
    if similarity <= BAND_REJECT:
        return "reject"
    return "escalate"


def _bucket_label(similarity: float) -> str:
    bucket = int(max(0.0, min(0.999, similarity)) * 20) / 20
    return f"[{bucket:.2f}, {bucket + 0.05:.2f})"

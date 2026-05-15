"""M5 public dataclasses — the embed/query stage's exported types.

``WorkEmbeddingInput`` is the per-Work tuple the input-string builder
consumes; ``IndexBuildResult`` summarises a :func:`build.build_index`
run (also serialised into the on-disk idmap JSON);
``CandidatePair`` is one row of ``embed-candidates.jsonl``; and
``EmbedStats`` is the end-of-run report :func:`build.query_candidates`
returns.

P-38 Phase D: extracted from m5/runner.py. No logic change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from bffi_pipeline.stages.m5.bands import Band


@dataclass(frozen=True)
class WorkEmbeddingInput:
    """Per-Work tuple consumed by the embedding-input-string builder."""

    work_uri: str
    creator: str | None
    title: str | None
    language: str | None
    year: str | None
    content_type: str | None


@dataclass(frozen=True)
class IndexBuildResult:
    """Summary of a build_index() run, written into the idmap JSON."""

    n_works: int
    ndim: int
    model_name: str
    hnsw_m: int
    hnsw_ef_construction: int
    hnsw_ef_search: int
    build_seconds: float
    index_path: str
    idmap_path: str

    def render(self) -> str:
        """Format the build result as paste-ready text for the embed CLI."""
        return (
            f"FAISS HNSW build complete\n"
            f"  works:               {self.n_works}\n"
            f"  ndim:                {self.ndim}\n"
            f"  model:               {self.model_name}\n"
            f"  HNSW M / efC / efS:  {self.hnsw_m} / "
            f"{self.hnsw_ef_construction} / {self.hnsw_ef_search}\n"
            f"  build seconds:       {self.build_seconds:.1f}\n"
            f"  index file:          {self.index_path}\n"
            f"  idmap file:          {self.idmap_path}"
        )


@dataclass(frozen=True)
class CandidatePair:
    """A single Work-pair candidate produced by the top-k retrieval pass."""

    work_a: str
    work_b: str
    similarity: float
    block_a: str
    block_b: str
    cross_block: bool
    band: Band


@dataclass
class EmbedStats:
    """Aggregate counts for an end-of-run embed-stats report."""

    n_works: int
    ndim: int
    model_name: str
    index_path: str
    similarity_buckets: Counter[str] = field(default_factory=Counter)
    band_counts: Counter[Band] = field(default_factory=Counter)
    cross_block_count: int = 0
    total_pairs: int = 0

    def render(self) -> str:
        """Format the embed-stats report as paste-ready text."""
        lines = [
            "Stage-2 embedding statistics",
            f"  works:        {self.n_works}",
            f"  ndim:         {self.ndim}",
            f"  model:        {self.model_name}",
            f"  index file:   {self.index_path}",
            f"  pairs total:  {self.total_pairs}",
        ]
        if self.total_pairs:
            lines.append("  band counts:")
            bands: tuple[Band, ...] = ("auto-merge", "escalate", "reject")
            for band in bands:
                count = self.band_counts.get(band, 0)
                pct = 100.0 * count / self.total_pairs
                lines.append(f"    {band:<10s}  {count:>8}  ({pct:5.1f}%)")
            cross_pct = 100.0 * self.cross_block_count / self.total_pairs
            lines.append(
                f"  cross-block hits: {self.cross_block_count} ({cross_pct:.1f}% of pairs)"
            )
        if self.similarity_buckets:
            lines.append("  top-k similarity histogram (0.05 buckets):")
            for bucket in sorted(self.similarity_buckets):
                lines.append(f"    {bucket}  {self.similarity_buckets[bucket]:>8}")
        return "\n".join(lines)

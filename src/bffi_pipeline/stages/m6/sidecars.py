"""M6 sidecar I/O — candidates JSONL loader, decisions row builder,
checkpoint mirror.

The batch driver reads M5's ``embed-candidates.jsonl`` and writes both
``judge-decisions.jsonl`` (one row per LLM call result) and a sibling
``.checkpoint`` JSON that captures resume state every
:data:`batch.CHECKPOINT_INTERVAL` pairs.

P-38 Phase D: extracted from m6/runner.py. No logic change.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from bffi_pipeline.stages.m6.outcome import JudgeOutcome

#: Per-record JSONL filename; ``judge_batch`` writes here.
DECISIONS_FILENAME: Final[str] = "judge-decisions.jsonl"

#: Suffix on the JSON checkpoint sibling that the batch driver mirrors
#: every ``CHECKPOINT_INTERVAL`` completed pairs so a resumed run picks
#: up exactly where the previous one left off.
CHECKPOINT_SUFFIX: Final[str] = ".checkpoint"

#: Band filter names — match the values M5's CandidatePair emits.
ESCALATE_BAND: Final[str] = "escalate"
AUTO_MERGE_BAND: Final[str] = "auto-merge"


@dataclass
class JudgeCheckpoint:
    """Persistent checkpoint state mirrored on disk between 100-pair flushes."""

    start_time: str
    last_completed_idx: int
    total_pairs: int
    cache_hits: int
    fresh_calls: int
    cascade_used: int

    def to_json(self) -> str:
        """Serialise this checkpoint to JSON for the on-disk mirror."""
        return _json.dumps(
            {
                "start_time": self.start_time,
                "last_completed_idx": self.last_completed_idx,
                "total_pairs": self.total_pairs,
                "cache_hits": self.cache_hits,
                "fresh_calls": self.fresh_calls,
                "cascade_used": self.cascade_used,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> JudgeCheckpoint:
        """Reconstruct a checkpoint from the JSON-on-disk mirror."""
        data = _json.loads(raw)
        return cls(
            start_time=data["start_time"],
            last_completed_idx=int(data["last_completed_idx"]),
            total_pairs=int(data["total_pairs"]),
            cache_hits=int(data["cache_hits"]),
            fresh_calls=int(data["fresh_calls"]),
            cascade_used=int(data.get("cascade_used", 0)),
        )


def _checkpoint_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + CHECKPOINT_SUFFIX)


def _serialise_decision(
    pair: dict[str, Any],
    outcome: JudgeOutcome,
) -> dict[str, Any]:
    """Build the per-row JSONL payload written to ``output_path``."""
    final = outcome.final
    return {
        "work_a": pair["work_a"],
        "work_b": pair["work_b"],
        "similarity": pair["similarity"],
        "block_a": pair.get("block_a"),
        "block_b": pair.get("block_b"),
        "cross_block": pair.get("cross_block"),
        "decision": final.decision,
        "confidence": final.confidence,
        "rationale": final.rationale,
        "matching_fields": list(final.matching_fields),
        "diverging_fields": list(final.diverging_fields),
        "used_cascade": outcome.used_cascade,
        "cascade": [
            {
                "stage": step.stage,
                "model": step.model_name,
                "decision": step.decision.decision,
                "confidence": step.decision.confidence,
                "cache_hit": step.cache_hit,
                "latency_seconds": step.latency_seconds,
            }
            for step in outcome.steps
        ],
    }


def _load_candidate_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load M5's ``embed-candidates.jsonl`` keeping only the escalate band."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Candidates JSONL not found at {path!s}. Run `bffi-pipeline embed` first."
        )
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {path!s}:{line_no}: {exc}") from exc
        if row.get("band") != ESCALATE_BAND:
            continue
        rows.append(row)
    return rows


def _load_auto_merge_candidates(path: Path) -> list[dict[str, Any]]:
    """Load M5's ``embed-candidates.jsonl`` keeping only the auto-merge band.

    These pairs cleared the M5 ceiling (similarity ≥ 0.90, spec § 6)
    and merge deterministically without an LLM call — the embedding
    similarity alone is the merge signal. Loading them separately
    from the escalate band keeps the LLM cascade path unchanged.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"Candidates JSONL not found at {path!s}. Run `bffi-pipeline embed` first."
        )
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {path!s}:{line_no}: {exc}") from exc
        if row.get("band") != AUTO_MERGE_BAND:
            continue
        rows.append(row)
    return rows


def _load_checkpoint(path: Path) -> JudgeCheckpoint | None:
    if not path.is_file():
        return None
    try:
        return JudgeCheckpoint.from_json(path.read_text(encoding="utf-8"))
    except ValueError, KeyError:
        return None


def _write_checkpoint(path: Path, ckpt: JudgeCheckpoint) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(ckpt.to_json(), encoding="utf-8")
    tmp.replace(path)

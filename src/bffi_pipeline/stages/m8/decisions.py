"""M8 decisions JSONL loader + conflict detection.

Reads M6's ``judge-decisions.jsonl`` into :class:`JudgeDecisionRow`
rows; :func:`_detect_conflicts` flags merge-groups whose union-find
membership contradicts a ``different_work`` edge.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from bffi_pipeline.stages.m8.schemas import GroupConflict, JudgeDecisionRow


def _load_decisions(path: Path) -> list[JudgeDecisionRow]:
    if not path.is_file():
        raise FileNotFoundError(
            f"M6 decisions JSONL not found at {path!s}. Run `bffi-pipeline judge` first."
        )
    rows: list[JudgeDecisionRow] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {path!s}:{line_no}: {exc}") from exc
        cascade = data.get("cascade") or []
        winning_model: str | None = None
        if cascade:
            winning_model = str(cascade[-1].get("model") or "") or None
        rows.append(
            JudgeDecisionRow(
                work_a=data["work_a"],
                work_b=data["work_b"],
                decision=data["decision"],
                confidence=float(data["confidence"]),
                used_cascade=bool(data.get("used_cascade", False)),
                winning_model=winning_model,
            )
        )
    return rows


def _detect_conflicts(
    groups: dict[str, list[str]],
    different_work_edges: Iterable[tuple[str, str]],
    same_work_edges: list[tuple[str, str]],
) -> list[GroupConflict]:
    """Return groups whose union-find membership contradicts a different_work edge."""
    member_to_root: dict[str, str] = {}
    for root, members in groups.items():
        for m in members:
            member_to_root[m] = root

    conflicts: list[GroupConflict] = []
    seen_roots: set[str] = set()
    for a, b in different_work_edges:
        root_a = member_to_root.get(a)
        root_b = member_to_root.get(b)
        if root_a is None or root_b is None:
            continue
        if root_a == root_b and root_a not in seen_roots:
            seen_roots.add(root_a)
            conflicts.append(
                GroupConflict(
                    members=sorted(groups[root_a]),
                    conflicting_pair=(a, b),
                    same_work_path=[edge for edge in same_work_edges if edge[0] in groups[root_a]],
                )
            )
    return conflicts

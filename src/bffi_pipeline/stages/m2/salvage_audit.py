"""M2 245$c salvage-cascade audit log.

Per-run sidecar that records every B1-LLM salvage call as a
candidate-shaped JSONL row, so the cataloguer-bundle stage can
build a salvage review queue without re-running the LLM
cascade against the BIBFRAME output.

Each row carries the call context the cataloguer needs to
review the salvage decision: helmet bib id, MARC 245$c verbatim
text, salvaged agents (post substring-check), the LLM's
rationale, and a ``cache_hit`` flag indicating whether the
decision was a fresh LLM call or a replay from
``synth-cache.sqlite``. Cache hits are audited too — the
decision influenced this run's BIBFRAME graph regardless of
when the LLM call was originally made.

Mirrors :mod:`bffi_pipeline.stages.m3.contrib_audit` in shape,
so the cataloguer-bundle dispatcher's auto-discovery loop
treats salvage like contrib + judge + picker.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: Filename M2 writes the audit log under in the run directory.
AUDIT_FILENAME: Final[str] = "salvage-candidates.jsonl"

#: Value stamped on every audit row's ``added_by`` field. Lets the
#: cataloguer tell salvage-mined candidates apart from contrib /
#: judge / picker.
ADDED_BY: Final[str] = "m2-salvage"


@dataclass(frozen=True)
class SalvageCallTelemetry:
    """Side-channel telemetry the extractor exposes after each
    ``.extract()`` call, so the audit writer can stamp the row with
    the LLM's rationale + the cache-hit flag.

    The Protocol's ``.extract()`` returns just the post-substring-
    check ``list[ParsedAgent]``; the rationale + cache_hit live
    here so the existing return contract isn't broken (and so test
    stubs can opt-in to audit-row content without rewiring).
    """

    rationale: str | None
    cache_hit: bool


def audit_log_path(run_dir: Path) -> Path:
    """Convention for where M2 writes the salvage audit log."""
    return run_dir / AUDIT_FILENAME


def reset_audit_log(path: Path) -> None:
    """Truncate the audit log (or create the empty file).

    Called at M2 stage start when the LLM salvage cascade is
    active so re-runs produce a coherent log instead of
    appending duplicates. The parent directory is created if
    needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def append_audit_row(
    path: Path,
    *,
    helmet_bib_id: str,
    c_subfield: str,
    salvaged_agents: Iterable[Any],
    telemetry: SalvageCallTelemetry | None,
    now: datetime | None = None,
) -> None:
    """Append one audit row per salvage cascade fire.

    ``salvaged_agents`` is the post-substring-check ``ParsedAgent``
    list (or an iterable of objects with ``.name`` / ``.role``).
    Objects are serialised to ``{name, role}`` dicts; any other
    attributes are dropped to keep the audit row stable across
    schema evolutions of ``ParsedAgent``.

    ``telemetry`` is the side-channel object the extractor stashes
    on itself after each ``.extract()`` call. ``None`` is OK — the
    row still lands, with ``rationale=null`` and ``cache_hit=null``
    (test stubs that don't expose telemetry).
    """
    seq = _next_seq(path)
    agents_payload: list[dict[str, Any]] = []
    for agent in salvaged_agents:
        agents_payload.append(
            {
                "name": getattr(agent, "name", str(agent)),
                "role": getattr(agent, "role", None),
            }
        )
    row: dict[str, Any] = {
        "id": f"cg-pending-{seq:04d}",
        "helmet_bib_id": helmet_bib_id,
        "c_subfield": c_subfield,
        "salvaged_agents": agents_payload,
        "rationale": telemetry.rationale if telemetry is not None else None,
        "cache_hit": telemetry.cache_hit if telemetry is not None else None,
        "added": (now or datetime.now(UTC)).date().isoformat(),
        "added_by": ADDED_BY,
        "notes": "",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")


def _next_seq(path: Path) -> int:
    """Count of non-blank lines already in ``path``, +1."""
    if not path.exists():
        return 1
    n = 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                n += 1
    return n + 1


__all__ = [
    "ADDED_BY",
    "AUDIT_FILENAME",
    "SalvageCallTelemetry",
    "append_audit_row",
    "audit_log_path",
    "reset_audit_log",
]

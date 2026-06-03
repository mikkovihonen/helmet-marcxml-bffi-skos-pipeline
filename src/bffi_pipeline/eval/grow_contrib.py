"""Gold-set growth for the M3 contributor-extraction cascade
(``gold/contrib.jsonl``).

P-06's sibling for P-39's gate: the M9 non-primary-contribution
reconciliation plan is gated on ``gold/contrib.jsonl`` reaching
≥ 30 cataloguer-vetted rows (currently 4 bootstrap rows). Manual
authoring is too slow; this module mines candidate rows from M2's
BIBFRAME output by running the same heuristic + LLM cascade M3
runs internally, and writes a JSONL file the cataloguer reviews,
picks from, fills in ``category`` + flags ``holdout``, and merges
into ``gold/contrib.jsonl`` by hand.

Two design points worth surfacing:

- **No M3 instrumentation.** We re-run ``contrib_extract.gather_inputs``
  + ``extract_contributions`` against the on-disk BIBFRAME RDFs
  M2 wrote. M3 itself doesn't keep an audit log of every cascade
  decision (only the transliteration-variant subset surfaces via
  ``contrib-variants.jsonl``); re-running is the cheapest way to
  capture every decision without changing M3's emit path.
- **Cost honesty.** Each cascade hit is one LLM call; the
  ``contrib_extract_llm.LangChainContribExtractor`` retry stack +
  per-call timeout apply. The Qwen3 8B warm path lands at ~10 s /
  call; expect ~10 minutes to walk a 3 000-record M2 corpus when
  the heuristic fires on ~10 % of records. The CLI's ``--limit``
  flag is the operator-side throttle.

The candidate JSONL row shape mirrors ``gold/contrib.jsonl``: an
``id`` placeholder the cataloguer overwrites at merge time, the
mined ``helmet_bib_id`` / ``c_subfield`` / ``existing_agents``,
the LLM-extracted ``expected_contributions``, a defaulted
``holdout: false`` + auto-suggested ``category`` (override-able by
the cataloguer), and provenance fields recording when + how the
candidate was mined.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from rdflib import Graph, URIRef

from bffi_pipeline.contrib_extract import (
    ExtractionInputs,
    extract_contributions,
    gather_inputs,
    heuristic_fires,
)
from bffi_pipeline.contrib_extract_llm import ContribExtractor
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m3.contrib_audit import (
    flatten_decision_contributions as _decision_to_contributions,
)
from bffi_pipeline.stages.m3.contrib_audit import (
    suggest_category as _suggest_category,
)
from bffi_pipeline.stages.m3.contributions import _read_helmet_bib_id

#: Default output path for the candidate JSONL — sits alongside
#: ``gold/contrib.jsonl`` so cataloguers find it without setup.
#: Distinct filename from the M6 grow-gold candidates so the two
#: review queues don't collide.
DEFAULT_CONTRIB_CANDIDATES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "gold" / "grow-candidates-contrib.jsonl"
)

#: ``added_by`` value stamped on every candidate. Lets the cataloguer
#: tell tool-generated candidates from bootstrap / volunteered rows.
ADDED_BY: Final[str] = "grow-gold-contrib"


@dataclass(frozen=True)
class ContribCandidate:
    """One candidate row written to ``gold/grow-candidates-contrib.jsonl``.

    Mirrors the ``gold/contrib.jsonl`` shape but with the cataloguer-
    fill-in fields defaulted: ``id`` placeholder (replaced on merge),
    ``category`` auto-suggested from the decision shape (override-able),
    ``holdout`` default ``false``, ``notes`` empty.
    """

    id: str
    category: str | None
    helmet_bib_id: str
    c_subfield: str
    existing_agents: list[str]
    expected_contributions: list[dict[str, Any]]
    holdout: bool
    added: str
    added_by: str
    notes: str


@dataclass
class GrowContribSummary:
    """Operator-facing summary printed by the CLI on completion."""

    bibframe_files_scanned: int = 0
    works_with_245c: int = 0
    heuristic_fires: int = 0
    llm_decisions_with_contributions: int = 0
    candidates_written: int = 0
    output_path: Path | None = None
    skipped_by_existing_id: int = 0
    by_category: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            "grow-gold-contrib summary",
            f"  bibframe files scanned:     {self.bibframe_files_scanned}",
            f"  works with 245$c:           {self.works_with_245c}",
            f"  heuristic-fires:            {self.heuristic_fires}",
            f"  decisions with ≥1 contrib:  {self.llm_decisions_with_contributions}",
            f"  candidates written:         {self.candidates_written}",
            f"  skipped (id already vetted): {self.skipped_by_existing_id}",
        ]
        if self.by_category:
            lines.append("  auto-suggested categories:")
            for cat, n in sorted(self.by_category.items()):
                lines.append(f"    {cat:35} {n}")
        if self.output_path is not None:
            lines.append(f"  output: {self.output_path}")
        return "\n".join(lines)


# --- Category suggestion --------------------------------------------------

# Canonical implementation lives in
# :mod:`bffi_pipeline.stages.m3.contrib_audit` so M3's per-fire audit
# log and this corpus-mining path stamp the same labels when their
# decision shapes match. ``_suggest_category`` is imported at the top
# of the module; the legacy module-level category-name constants are
# kept here for any callers that imported them directly.
_CATEGORY_PURE_NEW_AGENT: Final[str] = "pure-new-agent"
_CATEGORY_TRANSLITERATION: Final[str] = "transliteration"
_CATEGORY_ROLE_CLASSIFICATION: Final[str] = "role-classification"
_CATEGORY_AMBIGUOUS: Final[str] = "ambiguous-multi-shape"


# --- Candidate generation -------------------------------------------------


def _iter_bibframe_files(bibframe_dir: Path) -> Iterator[Path]:
    """Yield ``*.rdf`` files in ``bibframe_dir`` in sorted order.

    Filters out files starting with ``_`` (M2's ``_errors.jsonl`` etc.).
    Sorted-order iteration makes a re-run produce the same row order
    in the candidate JSONL, which keeps `git diff` between runs tight.
    """
    if not bibframe_dir.is_dir():
        return
    for path in sorted(bibframe_dir.glob("*.rdf")):
        if not path.name.startswith("_"):
            yield path


def _load_existing_vetted_ids(gold_path: Path) -> set[str]:
    """Read ``gold/contrib.jsonl`` and return the set of
    ``helmet_bib_id`` values already in the vetted gold set.

    The candidate generator skips records whose bib has already been
    reviewed — re-running it doesn't ask the cataloguer to re-vet a
    case they already merged.
    """
    if not gold_path.is_file():
        return set()
    seen: set[str] = set()
    with gold_path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            bib_id = row.get("helmet_bib_id")
            if bib_id:
                seen.add(str(bib_id))
    return seen


def _candidate_from_decision(
    *,
    inputs: ExtractionInputs,
    bib_id: str,
    contributions: list[dict[str, Any]],
    today: str,
    sequence: int,
) -> ContribCandidate:
    return ContribCandidate(
        id=f"cg-pending-{sequence:04d}",
        category=_suggest_category(contributions),
        helmet_bib_id=bib_id,
        c_subfield=inputs.c_subfield,
        existing_agents=list(inputs.existing_agent_labels),
        expected_contributions=contributions,
        holdout=False,
        added=today,
        added_by=ADDED_BY,
        notes="",
    )


# ``_decision_to_contributions`` is re-exported from
# :mod:`bffi_pipeline.stages.m3.contrib_audit` at the top of the
# module so audit-log rows and corpus-mined rows produce
# byte-identical shape when their decisions match.


def generate_candidates(
    *,
    bibframe_dir: Path,
    output_path: Path,
    extractor: ContribExtractor | None,
    limit: int | None = None,
    existing_gold_path: Path | None = None,
) -> GrowContribSummary:
    """Walk ``bibframe_dir/*.rdf`` and write candidate rows.

    For each :class:`bf:Work` in each graph: run the contrib-extract
    heuristic + (if an extractor is supplied) the LLM cascade. When
    the cascade returns at least one contribution, emit one
    :class:`ContribCandidate` row to ``output_path``.

    ``limit`` caps the number of BIBFRAME files scanned (operator-
    side throttle for cost-bounded smoke tests).
    ``existing_gold_path`` opt-in dedup against ``gold/contrib.jsonl``;
    bib IDs already in the vetted gold file are skipped without
    re-invoking the LLM.

    Without an ``extractor``, this function walks the corpus and
    measures heuristic-fire rate without spending LLM time — useful
    for cost projection. No candidates are written in that case.
    """
    summary = GrowContribSummary(output_path=output_path)
    today = datetime.now(UTC).date().isoformat()
    vetted_ids = (
        _load_existing_vetted_ids(existing_gold_path) if existing_gold_path is not None else set()
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sequence = 0

    with output_path.open("w", encoding="utf-8") as out_fh:
        for i, rdf_path in enumerate(_iter_bibframe_files(bibframe_dir)):
            if limit is not None and i >= limit:
                break
            summary.bibframe_files_scanned += 1
            g = Graph()
            try:
                g.parse(str(rdf_path), format="xml")
            except Exception:
                # Bad RDF — count as scanned, skip. The bench's M2 already
                # warned about cataloguer-data-quality URI escapes.
                continue
            for work in g.subjects(V.RDF.type, V.BF.Work):
                if not isinstance(work, URIRef):
                    continue
                inputs = gather_inputs(g, work)
                if inputs is None:
                    continue
                summary.works_with_245c += 1
                bib_id = _read_helmet_bib_id(g, work)
                if bib_id is None or bib_id in vetted_ids:
                    if bib_id is not None:
                        summary.skipped_by_existing_id += 1
                    continue
                # Heuristic gate — the cascade only fires on records
                # with 245$c tokens uncovered by existing 100/700 labels.
                # Counted separately from the cascade outcome so
                # ``--no-llm-cascade`` (cost-projection mode) still
                # reports a meaningful heuristic-fire rate.
                if not heuristic_fires(inputs):
                    continue
                summary.heuristic_fires += 1
                if extractor is None:
                    # Measurement-only mode; no candidates to write.
                    continue
                decision = extract_contributions(inputs, extractor=extractor)
                if decision is None or not decision.contributions:
                    continue
                contribs = _decision_to_contributions(decision)
                summary.llm_decisions_with_contributions += 1
                sequence += 1
                candidate = _candidate_from_decision(
                    inputs=inputs,
                    bib_id=bib_id,
                    contributions=contribs,
                    today=today,
                    sequence=sequence,
                )
                out_fh.write(json.dumps(_candidate_to_jsonable(candidate), ensure_ascii=False))
                out_fh.write("\n")
                summary.candidates_written += 1
                cat = candidate.category or "uncategorised"
                summary.by_category[cat] = summary.by_category.get(cat, 0) + 1

    return summary


def _candidate_to_jsonable(c: ContribCandidate) -> dict[str, Any]:
    """Dataclass → dict for JSONL output. We hand-build instead of
    ``asdict`` so the key order in the output file matches the existing
    ``gold/contrib.jsonl`` rows."""
    return {
        "id": c.id,
        "category": c.category,
        "helmet_bib_id": c.helmet_bib_id,
        "c_subfield": c.c_subfield,
        "existing_agents": c.existing_agents,
        "expected_contributions": c.expected_contributions,
        "holdout": c.holdout,
        "added": c.added,
        "added_by": c.added_by,
        "notes": c.notes,
    }


def candidates_from_iterable(
    decisions: Iterable[tuple[str, ExtractionInputs, list[dict[str, Any]]]],
    *,
    today: str | None = None,
) -> list[ContribCandidate]:
    """Pure-functional candidate-row builder, useful in tests + when
    decisions come from a non-graph source (e.g. a fixture)."""
    if today is None:
        today = datetime.now(UTC).date().isoformat()
    rows: list[ContribCandidate] = []
    for i, (bib_id, inputs, contributions) in enumerate(decisions, start=1):
        rows.append(
            _candidate_from_decision(
                inputs=inputs,
                bib_id=bib_id,
                contributions=contributions,
                today=today,
                sequence=i,
            )
        )
    return rows


__all__ = [
    "ADDED_BY",
    "DEFAULT_CONTRIB_CANDIDATES_PATH",
    "ContribCandidate",
    "GrowContribSummary",
    "candidates_from_iterable",
    "generate_candidates",
]

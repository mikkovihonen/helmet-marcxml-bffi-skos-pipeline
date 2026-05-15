"""Gold-set growth from human-override SPARQL query (M12 phase 3).

Per spec § 9: "the 'humans overrode the LLM' SPARQL query is your
gold-set growth pipeline. Run monthly, present candidates, add
confirmed ones with `category` filled in. New cases default to
``holdout: false``; flip the flag explicitly."

The query lives at ``sparql/queries/grow_overrides.rq`` so the
canonical Fuseki SPARQL can be tweaked without code changes.

Implementation contract:
- Reads the provenance + bffi-works named graphs from a running
  Fuseki. The bffi-works graph is needed because the override
  Activity carries ``prov:used`` to the *raw* Work URIs; the
  cataloguer-friendly creator + title + language values live on those
  Works. ``OPTIONAL`` joins keep the query tolerant when one side has
  been compacted or the bffi-works graph wasn't loaded with the same
  named-graph URI as expected.
- Emits one ``GoldCandidate`` per pair, deliberately *not* a
  ``GoldCase``: ``category`` is set to ``None`` because only a
  cataloguer can classify the override; ``expected`` is the inverse of
  the original LLM decision (the cataloguer disagreed). The user
  reviews ``gold/grow-candidates.jsonl``, picks the cases worth
  promoting, fills in ``category``, and merges into ``gold/gold.jsonl``
  by hand.
- HTTP work goes through an injectable ``httpx.Client`` so unit tests
  use ``httpx.MockTransport`` to feed canned SPARQL JSON responses.

The CLI subcommand ``bffi-pipeline grow-gold`` writes the candidate
JSONL and prints a one-line summary.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.m10 import load as load_stage

#: Repo-relative path to the SPARQL query the grow stage runs.
DEFAULT_GROW_QUERY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "sparql" / "queries" / "grow_overrides.rq"
)

#: Default output path for candidate JSONL (cataloguer reviews this and
#: hand-merges into gold/gold.jsonl). Sits alongside the canonical gold
#: file so cataloguers find it without setup.
DEFAULT_CANDIDATES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "gold" / "grow-candidates.jsonl"
)


@dataclass(frozen=True)
class GoldCandidate:
    """One candidate row written to ``gold/grow-candidates.jsonl``.

    Mirrors :class:`bffi_pipeline.eval.gold_set.GoldCase` minus the
    fields a cataloguer must fill in by hand: ``category`` is left
    ``None``, ``expected`` is the *cataloguer's* decision (the inverse
    of the LLM decision they overrode), ``holdout`` defaults to
    ``False``. ``notes`` carries the review note + the original LLM
    rationale so reviewers don't need to cross-reference Fuseki.
    """

    id: str
    expected: str
    category: str | None = None
    holdout: bool = False
    added: str | None = None
    added_by: str = "grow-from-overrides"
    notes: str | None = None
    record_a: dict[str, Any] = field(default_factory=dict)
    record_b: dict[str, Any] = field(default_factory=dict)


def grow_query_text(path: Path | None = None) -> str:
    """Return the SPARQL query body that powers the override scan."""
    target = path or DEFAULT_GROW_QUERY_PATH
    if not target.is_file():
        raise FileNotFoundError(f"Grow-overrides query not found at {target!s}; restore from git.")
    return target.read_text(encoding="utf-8")


def _binding_value(row: dict[str, Any], key: str) -> str | None:
    """Pull ``row[key]['value']`` if present; tolerate missing keys."""
    cell = row.get(key)
    if not isinstance(cell, dict):
        return None
    value = cell.get("value")
    return str(value) if value is not None else None


def _bib_id_from_value(value: str | None) -> str | None:
    """Extract a Helmet bib_id from an identifier ``rdf:value`` literal."""
    return value.strip() if value else None


def _record_payload(
    *,
    creator: str | None,
    title: str | None,
    language: str | None,
    helmet_bib_id: str | None,
) -> dict[str, Any]:
    """Build the ``record_a`` / ``record_b`` JSON payload from SPARQL bindings."""
    out: dict[str, Any] = {}
    if creator is not None:
        out["creator"] = creator
    if title is not None:
        out["title"] = title
    if language is not None:
        out["language"] = language
    if helmet_bib_id is not None:
        out["helmet_bib_id"] = helmet_bib_id
    return out


def _candidate_from_binding(row: dict[str, Any]) -> GoldCandidate:
    """Coerce one SPARQL row into a :class:`GoldCandidate`."""
    activity = _binding_value(row, "activity") or ""
    decision = _binding_value(row, "decision") or "uncertain"
    rationale = _binding_value(row, "rationale") or ""
    review_note = _binding_value(row, "reviewNote") or ""
    review_decision = _binding_value(row, "reviewDecision") or "overridden"

    # Cataloguer's call is the inverse of what the LLM committed to.
    # If the LLM said same_work, the override means different_work, and
    # vice versa. uncertain inputs flip to same_work as the most useful
    # default — the cataloguer can correct in the JSONL.
    inverse = {"same_work": "different_work", "different_work": "same_work"}
    expected = inverse.get(decision, "same_work")

    record_a = _record_payload(
        creator=_binding_value(row, "creatorA"),
        title=_binding_value(row, "titleA"),
        language=_binding_value(row, "languageA"),
        helmet_bib_id=_bib_id_from_value(_binding_value(row, "bibIdA")),
    )
    record_b = _record_payload(
        creator=_binding_value(row, "creatorB"),
        title=_binding_value(row, "titleB"),
        language=_binding_value(row, "languageB"),
        helmet_bib_id=_bib_id_from_value(_binding_value(row, "bibIdB")),
    )

    notes_parts: list[str] = []
    if review_note:
        notes_parts.append(f"reviewer: {review_note}")
    if rationale:
        notes_parts.append(f"original LLM rationale: {rationale}")
    notes_parts.append(f"original LLM decision: {decision} (review: {review_decision})")

    # The Activity URI is unique per decision — use its tail as the
    # candidate id so re-runs produce stable filenames.
    activity_id = activity.rsplit("/", 1)[-1] or activity
    return GoldCandidate(
        id=f"grow-{activity_id}",
        expected=expected,
        notes="; ".join(notes_parts) if notes_parts else None,
        record_a=record_a,
        record_b=record_b,
    )


def fetch_override_candidates(
    *,
    fuseki_url: str | None = None,
    http_client: httpx.Client | None = None,
    query_path: Path | None = None,
) -> list[GoldCandidate]:
    """Run the SPARQL query and return one :class:`GoldCandidate` per binding.

    ``fuseki_url`` defaults to ``BFFI_FUSEKI_URL``. ``http_client`` is
    an injection point for tests; production callers pass ``None`` and
    a fresh ``httpx.Client`` is created per call.
    """
    settings = get_settings()
    target_url = fuseki_url or settings.fuseki_url
    query = grow_query_text(query_path)

    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        bindings = load_stage.run_select(client, fuseki_url=target_url, query=query)
    finally:
        if own_client:
            client.close()
    return [_candidate_from_binding(row) for row in bindings]


def write_candidates(path: Path, candidates: list[GoldCandidate]) -> None:
    """Write ``candidates`` to ``path`` as JSONL, one row per candidate.

    Sorted by id so re-running grow against an unchanged Fuseki state
    produces a byte-stable file (the activity URI is ULID-derived so
    sorted-by-id is roughly chronological — most-recent overrides at
    the top would be nicer, but byte-stability + a known sort beats
    "newest first" when reviewers use git diff).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(candidates, key=lambda c: c.id)
    payload = "\n".join(json.dumps(asdict(c), ensure_ascii=False) for c in rows)
    if payload:
        payload += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


@dataclass
class GrowResult:
    """End-of-run summary surfaced by the CLI."""

    candidates_written: int
    output_path: str

    def render(self) -> str:
        """Format this result as paste-ready text for the grow CLI."""
        return (
            f"M12 grow complete\n"
            f"  candidates written:      {self.candidates_written:,}\n"
            f"  candidate JSONL:         {self.output_path}"
        )


def grow(
    *,
    fuseki_url: str | None = None,
    http_client: httpx.Client | None = None,
    query_path: Path | None = None,
    output_path: Path | None = None,
) -> GrowResult:
    """End-to-end: fetch overrides, write candidate JSONL, return summary."""
    candidates = fetch_override_candidates(
        fuseki_url=fuseki_url,
        http_client=http_client,
        query_path=query_path,
    )
    target_path = output_path or DEFAULT_CANDIDATES_PATH
    write_candidates(target_path, candidates)
    return GrowResult(
        candidates_written=len(candidates),
        output_path=str(target_path),
    )


__all__ = [
    "DEFAULT_CANDIDATES_PATH",
    "DEFAULT_GROW_QUERY_PATH",
    "GoldCandidate",
    "GrowResult",
    "fetch_override_candidates",
    "grow",
    "grow_query_text",
    "write_candidates",
]

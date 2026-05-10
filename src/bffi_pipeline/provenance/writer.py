"""On-disk Turtle writer for the provenance named graph (spec § 8 / M7).

Until M10 routes the provenance graph into Fuseki, the pipeline
persists it as two sibling Turtle files under ``BFFI_DATA_DIR``:

* ``provenance.ttl`` — the main
  ``<http://urn.fi/URN:NBN:fi:bib:graph:provenance>`` named graph.
  Holds every ``bffi-prov:WorkMergeDecision``, ``bffi-prov:HumanReview``,
  and ``prov:SoftwareAgent`` Activity / agent block.
* ``provenance-meta.ttl`` — the auxiliary
  ``<http://urn.fi/URN:NBN:fi:bib:graph:provenance-meta>`` graph.
  Holds a single triple recording the date of the last compaction so
  the CLI can warn when the structured fields are stale (90-day
  policy per spec § 8).

The writer is intentionally minimal: it keeps an in-memory
``rdflib.Graph`` and serializes it to disk on :meth:`flush` (or on
context-manager exit) using tmp-then-rename for crash safety. For a
~50-100 k-decision production run that's well within RAM, and a
single flush on completion is enough; the M6 batch driver also
provides crash recovery via its own checkpoint mechanism.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from rdflib import Graph, Literal, URIRef

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import logger as P
from bffi_pipeline.provenance import vocab as V

#: Named-graph URIs from spec § 8. Stored on the meta-graph triple as the
#: subject so consumers can link the meta sentinel to the graph it
#: describes.
PROVENANCE_GRAPH_URI: Final[str] = "http://urn.fi/URN:NBN:fi:bib:graph:provenance"
PROVENANCE_META_GRAPH_URI: Final[str] = "http://urn.fi/URN:NBN:fi:bib:graph:provenance-meta"

#: Default Turtle filenames under ``BFFI_DATA_DIR``.
PROVENANCE_FILENAME: Final[str] = "provenance.ttl"
PROVENANCE_META_FILENAME: Final[str] = "provenance-meta.ttl"

#: Compaction policy from spec § 8 / BUILD_PLAN M7. ``rawResponse`` literals
#: older than this become eligible for stripping; CLI startup nags when
#: ``lastCompactedAt`` is older than this.
COMPACTION_AGE_DAYS: Final[int] = 90


def default_provenance_path() -> Path:
    """Return ``<BFFI_DATA_DIR>/provenance.ttl`` from the live Settings."""
    return get_settings().data_dir / PROVENANCE_FILENAME


def default_provenance_meta_path() -> Path:
    """Return ``<BFFI_DATA_DIR>/provenance-meta.ttl`` from the live Settings."""
    return get_settings().data_dir / PROVENANCE_META_FILENAME


def _atomic_serialize(graph: Graph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    graph.serialize(destination=str(tmp), format="turtle")
    tmp.replace(path)


# --- Main provenance writer ----------------------------------------------


class ProvenanceWriter:
    """In-memory accumulator that flushes the provenance graph to Turtle.

    Use as a context manager so the final ``flush`` is guaranteed even on
    exception::

        with ProvenanceWriter() as writer:
            writer.add_software_agent(model_id="qwen3:32b-q4_K_M", ...)
            writer.add_merge_decision(...)
        # Turtle file written on scope exit.

    On construction the existing ``provenance.ttl`` is parsed back so
    re-runs append rather than overwrite. The M6 batch driver wires
    :meth:`add_merge_decision` as the ``decision_callback``.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_provenance_path()
        self.graph = Graph()
        self._bind_prefixes()
        if self.path.is_file():
            self.graph.parse(str(self.path), format="turtle")

    def _bind_prefixes(self) -> None:
        self.graph.bind("prov", V.PROV)
        self.graph.bind("bffi", V.BFFI)
        self.graph.bind("bffi-prov", V.BFFI_PROV)
        self.graph.bind("bib", V.BIB)
        self.graph.bind("bf", V.BF)
        self.graph.bind("rdfs", V.RDFS)
        self.graph.bind("xsd", V.XSD)

    # --- Activity / agent emission ---------------------------------------

    def add_software_agent(self, **kwargs: Any) -> URIRef:
        """Append one ``prov:SoftwareAgent`` block.

        See :func:`provenance.logger.log_software_agent` for the field set.
        """
        return P.log_software_agent(self.graph, **kwargs)

    def add_merge_decision(self, **kwargs: Any) -> URIRef:
        """Append one ``bffi-prov:WorkMergeDecision`` Activity. Returns its URI."""
        return P.log_merge_decision(self.graph, **kwargs)

    def add_review(self, **kwargs: Any) -> URIRef:
        """Append one ``bffi-prov:HumanReview`` Activity. Returns its URI."""
        return P.log_review(self.graph, **kwargs)

    # --- Persistence -----------------------------------------------------

    def flush(self) -> None:
        """Serialise the in-memory graph to ``self.path`` atomically."""
        _atomic_serialize(self.graph, self.path)

    def __enter__(self) -> ProvenanceWriter:
        return self

    def __exit__(self, *args: object) -> None:
        with suppress(Exception):
            self.flush()


# --- Meta-graph helpers (lastCompactedAt sentinel) -----------------------


def _provenance_graph_subject() -> URIRef:
    return URIRef(PROVENANCE_GRAPH_URI)


def read_last_compacted_at(meta_path: Path | None = None) -> datetime | None:
    """Return ``bffi-prov:lastCompactedAt`` from the meta graph, or ``None``."""
    target = meta_path or default_provenance_meta_path()
    if not target.is_file():
        return None
    g = Graph()
    try:
        g.parse(str(target), format="turtle")
    except Exception:
        return None
    for o in g.objects(_provenance_graph_subject(), V.lastCompactedAt):
        if isinstance(o, Literal):
            try:
                return datetime.fromisoformat(str(o))
            except ValueError:
                return None
    return None


def write_last_compacted_at(
    moment: datetime,
    meta_path: Path | None = None,
) -> None:
    """Replace the meta graph's ``lastCompactedAt`` value with ``moment``."""
    target = meta_path or default_provenance_meta_path()
    g = Graph()
    g.bind("bffi-prov", V.BFFI_PROV)
    g.bind("xsd", V.XSD)
    g.add(
        (
            _provenance_graph_subject(),
            V.lastCompactedAt,
            Literal(moment.isoformat(), datatype=V.XSD.dateTime),
        )
    )
    _atomic_serialize(g, target)


# --- Compaction ----------------------------------------------------------


def compact_provenance(
    *,
    older_than_days: int = COMPACTION_AGE_DAYS,
    provenance_path: Path | None = None,
    meta_path: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Strip ``bffi-prov:rawResponse`` from Activities older than ``older_than_days``.

    Structured fields (``decision``, ``confidence``, ``rationale``,
    ``promptHash``, …) survive the cull. Returns the number of triples
    removed so the CLI can report progress. Updates the meta graph's
    ``lastCompactedAt`` sentinel either way — even when zero triples
    matched, recording the run silences the staleness warning.
    """
    target = provenance_path or default_provenance_path()
    g = Graph()
    g.bind("bffi-prov", V.BFFI_PROV)
    if target.is_file():
        g.parse(str(target), format="turtle")

    cutoff = (now or datetime.now(UTC)) - _days(older_than_days)
    removed = 0
    for activity, started in list(g.subject_objects(V.PROV.startedAtTime)):
        if not isinstance(started, Literal):
            continue
        try:
            started_at = datetime.fromisoformat(str(started))
        except ValueError:
            continue
        if started_at >= cutoff:
            continue
        for raw in list(g.objects(activity, V.rawResponse)):
            g.remove((activity, V.rawResponse, raw))
            removed += 1

    _atomic_serialize(g, target)
    write_last_compacted_at(now or datetime.now(UTC), meta_path)
    return removed


def _days(n: int) -> timedelta:
    return timedelta(days=n)


# --- Stale-warning helper -----------------------------------------------


def stale_provenance_warning(
    *,
    meta_path: Path | None = None,
    older_than_days: int = COMPACTION_AGE_DAYS,
    provenance_path: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return a human-readable warning string when the provenance graph is stale.

    Suppressed silently when no provenance file exists (early-milestone
    or first-run case). Fires when the file exists *and* either the
    meta sentinel is missing or its date is older than the policy.
    """
    prov_target = provenance_path or default_provenance_path()
    if not prov_target.is_file():
        return None
    last = read_last_compacted_at(meta_path)
    moment = now or datetime.now(UTC)
    if last is None:
        return (
            f"warning: provenance file at {prov_target!s} has never been compacted. "
            f"Run `bffi-pipeline provenance compact --older-than {older_than_days}d`."
        )
    age = moment - last
    if age > _days(older_than_days):
        return (
            f"warning: provenance compaction is stale "
            f"(last run {last.date().isoformat()}, "
            f"{age.days} days ago). "
            f"Run `bffi-pipeline provenance compact --older-than {older_than_days}d`."
        )
    return None


__all__ = [
    "COMPACTION_AGE_DAYS",
    "PROVENANCE_FILENAME",
    "PROVENANCE_GRAPH_URI",
    "PROVENANCE_META_FILENAME",
    "PROVENANCE_META_GRAPH_URI",
    "ProvenanceWriter",
    "compact_provenance",
    "default_provenance_meta_path",
    "default_provenance_path",
    "read_last_compacted_at",
    "stale_provenance_warning",
    "write_last_compacted_at",
]

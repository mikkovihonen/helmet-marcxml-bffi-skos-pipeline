"""Stage M4: Stage 1 deterministic blocking — graph extraction + statistics.

Walks a combined BFFI + BIBFRAME graph, computes the rule-based blocking
key from spec § 6 Stage 1 for every BFFI Work, and reports block-size
statistics. The pure key-composition logic lives in
:mod:`bffi_pipeline.blocking`; this module only handles the graph and
the aggregate counts.

The key shrinks candidate-pair generation in M5/M6 to within-block
comparisons, eliminating >99% of the n² space before any embedding or
LLM runs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.blocking import compute_blocking_key
from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V

_LANG_URI_PREFIX = "http://id.loc.gov/vocabulary/contentTypes/"


# --- Graph extraction -----------------------------------------------------


@dataclass(frozen=True)
class WorkBlockingInput:
    """Per-Work tuple: BFFI Work URI + its blocking-key inputs."""

    work_uri: str
    creator: str | None
    title: str | None
    content_type: str | None


def _first_pref_label(graph: Graph, subject: URIRef) -> str | None:
    for o in graph.objects(subject, V.SKOS.prefLabel):
        if isinstance(o, Literal):
            return str(o)
    return None


def _primary_agent_uris(graph: Graph, work: URIRef) -> list[URIRef]:
    """Return the URIs of agents linked from ``work``'s primary contribution(s)."""
    agents: list[URIRef] = []
    for contrib in graph.objects(work, V.BFFI.contribution):
        types = set(graph.objects(contrib, RDF.type))
        if V.BFFI.PrimaryContribution not in types:
            continue
        for agent in graph.objects(contrib, V.BFFI.agent):
            if isinstance(agent, URIRef):
                agents.append(agent)
    return agents


def _content_type_for(graph: Graph, work: URIRef) -> str | None:
    """Return any one ``bffi:content`` URI fragment for ``work``'s expressions."""
    for expr in graph.objects(work, V.BFFI.hasExpression):
        if not isinstance(expr, URIRef):
            continue
        for ct in graph.objects(expr, V.BFFI.content):
            if isinstance(ct, URIRef):
                value = str(ct)
                if value.startswith(_LANG_URI_PREFIX):
                    return value[len(_LANG_URI_PREFIX) :]
                return value.rsplit("/", 1)[-1]
    return None


def extract_blocking_inputs(graph: Graph) -> Iterator[WorkBlockingInput]:
    """Walk a combined BFFI + BIBFRAME graph and yield per-Work blocking inputs.

    The agent label comes from the BIBFRAME side (``rdfs:label`` on the
    agent URI emitted by marc2bibframe2). The title comes from the BFFI
    Work's ``skos:prefLabel``; the content type from any of its
    Expressions' ``bffi:content``.
    """
    for work in graph.subjects(RDF.type, V.BFFI.Work):
        if not isinstance(work, URIRef):
            continue
        title = _first_pref_label(graph, work)
        creator: str | None = None
        for agent in _primary_agent_uris(graph, work):
            for label in graph.objects(agent, RDFS.label):
                if isinstance(label, Literal):
                    creator = str(label)
                    break
            if creator:
                break
        yield WorkBlockingInput(
            work_uri=str(work),
            creator=creator,
            title=title,
            content_type=_content_type_for(graph, work),
        )


def _iter_corpus_files(base: Path) -> tuple[list[Path], list[Path]]:
    """Return (bffi-turtle-files, bibframe-rdf-files) for ``base``.

    ``base`` may itself be a single ``.ttl`` file (used standalone) or a
    directory. If a directory, ``base/bffi/*.ttl`` and ``base/bibframe/*.rdf``
    are scanned (matching the layout produced by M2 + M3).
    """
    if base.is_file():
        return [base], []
    bffi = sorted((base / "bffi").glob("*.ttl")) if (base / "bffi").exists() else []
    bibframe = sorted(
        p
        for p in (base / "bibframe").glob("*.rdf")
        if (base / "bibframe").exists() and not p.name.startswith("_")
    )
    return bffi, bibframe


def load_corpus(base: Path | None = None) -> Graph:
    """Load every BFFI Turtle and BIBFRAME RDF/XML under ``base`` into one Graph.

    Defaults to ``BFFI_DATA_DIR``. The combined graph is what
    :func:`extract_blocking_inputs` walks.
    """
    base = base or get_settings().data_dir
    g = Graph()
    bffi_files, bibframe_files = _iter_corpus_files(base)
    for path in bffi_files:
        g.parse(str(path), format="turtle")
    for path in bibframe_files:
        g.parse(str(path), format="xml")
    return g


# --- Block-size statistics ------------------------------------------------


@dataclass(frozen=True)
class BlockingStats:
    """Aggregate counts for a workkey-stats run."""

    total_works: int
    blocks: dict[str, list[str]]

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def size_distribution(self) -> Counter[int]:
        return Counter(len(works) for works in self.blocks.values())

    def render(self) -> str:
        lines = [
            "Stage-1 blocking-key statistics",
            f"  works:  {self.total_works}",
            f"  blocks: {self.block_count}",
        ]
        if self.total_works:
            singletons = self.size_distribution.get(1, 0)
            lines.append(f"  singleton blocks: {singletons}")
        if self.size_distribution:
            lines.append("  block-size histogram:")
            for size, count in sorted(self.size_distribution.items()):
                lines.append(f"    size={size:>4}  count={count}")
        if self.blocks:
            largest = max(self.blocks.items(), key=lambda kv: len(kv[1]))
            lines.append(f"  largest block: '{largest[0]}' ({len(largest[1])} Works)")
        return "\n".join(lines)


def compute_blocks(graph: Graph) -> BlockingStats:
    """Group Work URIs in ``graph`` by their blocking key and return stats."""
    blocks: dict[str, list[str]] = {}
    total = 0
    for entry in extract_blocking_inputs(graph):
        total += 1
        key = compute_blocking_key(
            {
                "creator": entry.creator,
                "title": entry.title,
                "content_type": entry.content_type,
            }
        )
        blocks.setdefault(key, []).append(entry.work_uri)
    return BlockingStats(total_works=total, blocks=blocks)


__all__ = [
    "BlockingStats",
    "WorkBlockingInput",
    "compute_blocks",
    "extract_blocking_inputs",
    "load_corpus",
]

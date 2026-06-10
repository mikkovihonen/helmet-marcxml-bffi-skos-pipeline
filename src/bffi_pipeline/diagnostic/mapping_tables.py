"""Auto-generate the Classes + Predicates tables in `docs/bf_to_bffi_mapping.md`.

The previous doc was hand-curated against the main-branch SPARQL CONSTRUCTs
and covered ~100 ``bf:*`` terms (the ones those CONSTRUCTs touched). On the
rewrite branch BIBFRAME 3.0.1 declares 450 terms total, so the hand-list
under-covered by ~70 % and its ``Used in`` column referenced SPARQL files
that no longer exist. The generator below replaces the hand-list with a
deterministic table derived from three sources together:

  - ``vocab/bibframe.rdf``   — the universe of ``bf:*`` terms.
  - ``vocab/lkd.rdf``        — BFFI's mapping relations to ``bf:*``
    (``owl:equivalentClass`` / ``equivalentProperty`` / ``rdfs:subPropertyOf`` /
    the ``bffi-meta:*Match`` predicates).
  - ``stages/bibframe_to_bffi/routings.py`` — the discriminator-routed
    terms (terms with no ``lkd.rdf`` relation but a per-instance handler
    at conversion time).

Status per row, in precedence order:

  - ``clean``           — 1-hop ``owl:equivalentClass`` / ``equivalentProperty``
    to a ``bffi:*`` URI (overrides any routing — handler is a no-op).
  - ``routed``          — no direct equivalence; a routing function in
    ``routings.py`` handles the term per-instance.
  - ``semantic-shift``  — the diagnostic's best path uses
    ``bffi-meta:broadMatch`` / ``closeMatch`` / ``narrowMatch`` /
    ``exactMatch``.
  - ``inherited``       — best path is a taxonomy walk
    (``rdfs:subClassOf`` / ``rdfs:subPropertyOf``); covered transitively
    by an ancestor's clean rename.
  - ``GAP``             — no path of any length and no routing handler.

Run via ``bffi-pipeline regenerate-mapping-tables`` or programmatically
via :func:`regenerate_mapping_tables`. CI re-runs in ``--check`` mode and
fails when the doc drifts from what the generator would emit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDFS

from bffi_pipeline.bibframe import load_ontology
from bffi_pipeline.config import get_settings
from bffi_pipeline.diagnostic.mapping_coverage import (
    BF_NAMESPACE,
    BFFI_NAMESPACE,
    PathStep,
    Reach,
    analyze_mapping_coverage,
    local_name,
)
from bffi_pipeline.stages.bibframe_to_bffi import routings as _r

#: Markers framing the auto-generated Classes block in the doc.
CLASSES_BEGIN_MARKER: Final[str] = "<!-- BEGIN AUTO: classes -->"
CLASSES_END_MARKER: Final[str] = "<!-- END AUTO: classes -->"

#: Markers framing the auto-generated Predicates block in the doc.
PREDICATES_BEGIN_MARKER: Final[str] = "<!-- BEGIN AUTO: predicates -->"
PREDICATES_END_MARKER: Final[str] = "<!-- END AUTO: predicates -->"

#: Default mapping-doc location.
DEFAULT_DOC_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "docs" / "bf_to_bffi_mapping.md"
)


# --- routing registry --------------------------------------------------------


@dataclass(frozen=True)
class _Routing:
    """One routing-callout entry from :mod:`...stages.bibframe_to_bffi.routings`.

    Set ``is_drop=True`` for routings that delete the triple rather than
    rewrite it — the auto-table renders these with the distinct
    ``**drop**`` status so the semantic is visible at a glance.
    """

    handler: str
    replacement: str
    link_kind: str
    is_drop: bool = False


def _identifier_scheme_replacement(bf_class: URIRef) -> str:
    token = _r._identifier_scheme_token(local_name(bf_class))
    return f"`bffi:Identifier` + `bffi:source <…/identifiers/{token}>`"


def _build_routing_registry() -> dict[URIRef, _Routing]:
    """One :class:`_Routing` per ``bf:*`` term that ``routings.py`` handles.

    The keys are URIRefs (not strings) so lookups against the diagnostic's
    Reach objects work directly.
    """
    ontology = load_ontology()
    registry: dict[URIRef, _Routing] = {}

    for bf_cls in ontology.class_descendants(_r.BF.Identifier):
        registry[bf_cls] = _Routing(
            handler="route_identifier_schemes",
            replacement=_identifier_scheme_replacement(bf_cls),
            link_kind="discriminator: BIBFRAME subclass → LoC scheme URI",
        )

    for bf_cls in _r.TITLE_VARIANT_CLASSES:
        registry[bf_cls] = _Routing(
            handler="route_title_variants",
            replacement="`bffi:Title` (anchor; subclass info preserved on `bffi:marcKey`)",
            link_kind="discriminator: marcKey",
        )

    registry[_r.BF.Hub] = _Routing(
        handler="route_hubs",
        replacement=(
            "`bffi:Work` / `bffi:Expression` / `bffi:Arrangement` / "
            "`bffi:SeriesExpression` (per marcKey)"
        ),
        link_kind="discriminator: marcKey",
    )

    for bf_cls, (work_pick, expr_pick) in _r.AXIS_DEFAULT_CLASSES.items():
        if work_pick == expr_pick:
            replacement = f"`bffi:{local_name(work_pick)}` (anchored — no axis split)"
            link_kind = "anchor downgrade (no Work/Expression alternative)"
        else:
            replacement = (
                f"`bffi:{local_name(work_pick)}` (Work-axis) / "
                f"`bffi:{local_name(expr_pick)}` (Expression-axis)"
            )
            link_kind = "discriminator: subject's Work-axis co-type signal"
        registry[bf_cls] = _Routing(
            handler="route_axis_default_classes",
            replacement=replacement,
            link_kind=link_kind,
        )

    registry[_r.BF.hasSeries] = _Routing(
        handler="route_series_links",
        replacement=(
            "`bffi:relation` → `bffi:Relation` bnode (`bffi:relationship <…/relationship/series>`)"
        ),
        link_kind="structured-relation chain",
    )

    for bf_pred, rel_uri in _r.RELATION_PREDICATE_ROUTINGS.items():
        if bf_pred == _r.BF.hasSeries:
            continue
        token = str(rel_uri).rsplit("/", 1)[-1]
        registry[bf_pred] = _Routing(
            handler="route_relation_predicates",
            replacement=(
                "`bffi:relation` → `bffi:Relation` bnode "
                f"(`bffi:relationship <…/relationship/{token}>`)"
            ),
            link_kind="structured-relation chain",
        )

    for bf_pred, (work_pick, expr_pick) in _r.AXIS_DEFAULT_PREDICATES.items():
        if work_pick == expr_pick:
            replacement = f"`bffi:{local_name(work_pick)}` (flat rename)"
            link_kind = "flat rename (no per-statement axis alternative)"
        else:
            replacement = (
                f"`bffi:{local_name(work_pick)}` (Work-axis) / "
                f"`bffi:{local_name(expr_pick)}` (Expression-axis)"
            )
            link_kind = "discriminator: subject's/object's Expression-axis signal"
        registry[bf_pred] = _Routing(
            handler="route_axis_default_predicates",
            replacement=replacement,
            link_kind=link_kind,
        )

    registry[_r.BF.provisionActivityStatement] = _Routing(
        handler="route_provision_activity_statement",
        replacement=("`bffi:date` (76X-78X linking-entry hubs) / `bffi:Note` (otherwise)"),
        link_kind="discriminator: URI fragment",
    )

    for bf_pred, bffi_forward in _r.INVERSE_PREDICATE_ROUTINGS.items():
        registry[bf_pred] = _Routing(
            handler="route_inverse_predicates",
            replacement=f"`bffi:{local_name(bffi_forward)}` (triple-swap: ?s → ?o)",
            link_kind="inverse-direction swap",
        )

    registry[_r.BF.noteFor] = _Routing(
        handler="route_note_for",
        replacement="`bffi:note` (triple-swap: ?note bf:noteFor ?subj → ?subj bffi:note ?note)",
        link_kind="inverse-direction swap",
    )

    registry[_r.BF.noteType] = _Routing(
        handler="drop_note_type",
        replacement=(
            "no BFFI carrier — BFFI 1.0.0 doesn't model literal note categorisation; "
            "candidate for a future BFFI extension via NLF"
        ),
        link_kind="no BFFI carrier; bounded data loss",
        is_drop=True,
    )

    registry[_r.BF.variantType] = _Routing(
        handler="drop_variant_type",
        replacement="redundant with the title-variant `bffi:marcKey` discriminator",
        link_kind="redundant signal",
        is_drop=True,
    )

    return registry


# --- row computation ---------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One row in either the Classes or Predicates table."""

    bf_term: URIRef
    status: str
    replacement: str
    link_kind: str
    also_satisfies: str  # used only on Classes; "—" for Predicates
    handler: str


def _format_relation(relation: str) -> str:
    """Render the BFS edge label in the doc's prefix style."""
    if relation in {"equivalentClass", "equivalentProperty", "sameAs"}:
        return f"owl:{relation}"
    if relation in {"subClassOf", "subPropertyOf"}:
        return f"rdfs:{relation}"
    if relation in {"broadMatch", "closeMatch", "exactMatch", "narrowMatch"}:
        return f"bffi-meta:{relation}"
    return relation


def _format_path(path: Sequence[PathStep]) -> str:
    return " → ".join(_format_relation(step.relation) for step in path)


def _is_semantic_shift_path(path: Sequence[PathStep]) -> bool:
    return any(
        step.relation in {"broadMatch", "closeMatch", "narrowMatch", "exactMatch"} for step in path
    )


def _also_satisfies(bffi_uri: URIRef, lkd_g: Graph) -> str:
    """The ``Also satisfies`` column: BIBFRAME classes recoverable by inference.

    Walks the BFFI side's ``rdfs:subClassOf`` chain upward from
    ``bffi_uri`` (which is the per-row best reach) and lists every
    BIBFRAME class an ancestor declares as ``owl:equivalentClass``.
    """
    ancestors: set[URIRef] = set()
    stack: list[URIRef] = [bffi_uri]
    while stack:
        node = stack.pop()
        for _, _, parent in lkd_g.triples((node, RDFS.subClassOf, None)):
            if (
                isinstance(parent, URIRef)
                and parent not in ancestors
                and str(parent).startswith(BFFI_NAMESPACE)
            ):
                ancestors.add(parent)
                stack.append(parent)

    bf_satisfied: set[URIRef] = set()
    for anc in ancestors:
        for _, _, o in lkd_g.triples((anc, OWL.equivalentClass, None)):
            if isinstance(o, URIRef) and str(o).startswith(BF_NAMESPACE):
                bf_satisfied.add(o)
    # Filter out the direct equivalence — it's already in the Replacement column.
    for _, _, o in lkd_g.triples((bffi_uri, OWL.equivalentClass, None)):
        if isinstance(o, URIRef) and o in bf_satisfied:
            bf_satisfied.discard(o)

    if not bf_satisfied:
        return "—"
    return ", ".join(f"`bf:{local_name(uri)}`" for uri in sorted(bf_satisfied, key=str))


def _compute_row(
    reach: Reach,
    *,
    routings: dict[URIRef, _Routing],
    lkd_graph: Graph,
    include_inheritance: bool,
) -> Row:
    """Classify one ``bf:*`` term and produce its table row."""
    bf_term = reach.bf_term
    routing = routings.get(bf_term)

    if reach.has_direct_equivalence:
        # Pick the cleanest path (best already prefers equivalentClass/Property).
        best = reach.best
        assert best is not None
        bffi_uri, path = best
        also_satisfies = _also_satisfies(bffi_uri, lkd_graph) if include_inheritance else "—"
        return Row(
            bf_term=bf_term,
            status="**clean**",
            replacement=f"`bffi:{local_name(bffi_uri)}`",
            link_kind=_format_path(path),
            also_satisfies=also_satisfies,
            handler="—",
        )

    if routing is not None:
        return Row(
            bf_term=bf_term,
            status="**drop**" if routing.is_drop else "**routed**",
            replacement=routing.replacement,
            link_kind=routing.link_kind,
            also_satisfies="—",
            handler=f"`{routing.handler}`",
        )

    if reach.bffi_paths:
        best = reach.best
        assert best is not None
        bffi_uri, path = best
        status = "*semantic-shift*" if _is_semantic_shift_path(path) else "*inherited*"
        also_satisfies = _also_satisfies(bffi_uri, lkd_graph) if include_inheritance else "—"
        return Row(
            bf_term=bf_term,
            status=status,
            replacement=f"`bffi:{local_name(bffi_uri)}`",
            link_kind=_format_path(path),
            also_satisfies=also_satisfies,
            handler="—",
        )

    return Row(
        bf_term=bf_term,
        status="**GAP**",
        replacement="—",
        link_kind="—",
        also_satisfies="—",
        handler="—",
    )


# --- table rendering ---------------------------------------------------------


def _render_classes_table(rows: Iterable[Row]) -> str:
    header = (
        "| `bf:` class | Status | `bffi:*` replacement | Link kind | "
        "Also satisfies (via inference) | Handler |\n"
        "|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| `bf:{local_name(r.bf_term)}` | {r.status} | {r.replacement} | "
        f"{r.link_kind} | {r.also_satisfies} | {r.handler} |\n"
        for r in rows
    )
    return header + body


def _render_predicates_table(rows: Iterable[Row]) -> str:
    header = (
        "| `bf:` predicate | Status | `bffi:*` replacement | Link kind | Handler |\n"
        "|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| `bf:{local_name(r.bf_term)}` | {r.status} | {r.replacement} | "
        f"{r.link_kind} | {r.handler} |\n"
        for r in rows
    )
    return header + body


def _status_tally(rows: Iterable[Row]) -> str:
    """One-line summary appended below each table."""
    tally: dict[str, int] = {}
    for row in rows:
        key = row.status.strip("*").strip("_")
        tally[key] = tally.get(key, 0) + 1
    total = sum(tally.values())
    parts = ", ".join(f"{tally[k]} {k}" for k in sorted(tally, key=lambda x: -tally[x]))
    return f"_{total} terms total: {parts}._\n"


# --- block builders ----------------------------------------------------------


@dataclass(frozen=True)
class GeneratedBlocks:
    """The two markdown blocks the generator emits."""

    classes_block: str
    predicates_block: str


def build_blocks(
    *,
    bibframe_path: Path | None = None,
    lkd_path: Path | None = None,
) -> GeneratedBlocks:
    """Compute the two markdown blocks (Classes + Predicates) for the doc.

    Pure: no filesystem writes. The caller decides where the output goes
    (in-place replacement via :func:`regenerate_mapping_tables`, or
    print-to-stdout via the CLI's ``--print`` mode).
    """
    settings = get_settings()
    bf_path = bibframe_path or settings.vocab_dir / "bibframe.rdf"
    lkd_p = lkd_path or settings.vocab_dir / "lkd.rdf"

    report = analyze_mapping_coverage(bibframe_path=bf_path, lkd_path=lkd_p)

    bibframe_g = Graph()
    bibframe_g.parse(bf_path, format="xml")
    lkd_g = Graph()
    lkd_g.parse(lkd_p, format="xml")

    ontology = load_ontology(bf_path)
    class_uris = ontology.classes
    obj_props = ontology.object_properties
    datatype_props = ontology.datatype_properties
    predicate_uris = obj_props | datatype_props

    routings = _build_routing_registry()

    # All Reach objects across the three buckets, keyed by bf_term.
    reach_by_term: dict[URIRef, Reach] = {r.bf_term: r for r in report.direct}
    reach_by_term.update({r.bf_term: r for r in report.indirect})
    for bf_term in report.unreachable:
        reach_by_term[bf_term] = Reach(bf_term=bf_term, bffi_paths={})

    class_rows: list[Row] = []
    predicate_rows: list[Row] = []
    for bf_term in sorted(reach_by_term, key=str):
        reach = reach_by_term[bf_term]
        if bf_term in class_uris:
            class_rows.append(
                _compute_row(
                    reach,
                    routings=routings,
                    lkd_graph=lkd_g,
                    include_inheritance=True,
                )
            )
        elif bf_term in predicate_uris:
            predicate_rows.append(
                _compute_row(
                    reach,
                    routings=routings,
                    lkd_graph=lkd_g,
                    include_inheritance=False,
                )
            )
        # Terms not declared as either class or property in BIBFRAME are
        # skipped — the diagnostic includes everything BIBFRAME declares,
        # but the doc tables split on the class/predicate distinction.

    classes_block = _render_classes_table(class_rows) + "\n" + _status_tally(class_rows)
    predicates_block = (
        _render_predicates_table(predicate_rows) + "\n" + _status_tally(predicate_rows)
    )
    return GeneratedBlocks(
        classes_block=classes_block,
        predicates_block=predicates_block,
    )


# --- doc-file mutation -------------------------------------------------------


def _replace_block(doc_text: str, begin_marker: str, end_marker: str, new_block: str) -> str:
    """Replace content between markers, preserving the markers themselves."""
    begin_idx = doc_text.find(begin_marker)
    end_idx = doc_text.find(end_marker)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        msg = f"could not locate markers in doc: begin={begin_marker!r}, end={end_marker!r}"
        raise ValueError(msg)
    before = doc_text[: begin_idx + len(begin_marker)]
    after = doc_text[end_idx:]
    return f"{before}\n\n{new_block}\n{after}"


def regenerate_mapping_tables(
    *,
    doc_path: Path | None = None,
    bibframe_path: Path | None = None,
    lkd_path: Path | None = None,
    check: bool = False,
) -> tuple[str, bool]:
    """Regenerate the Classes + Predicates tables in the mapping doc.

    Returns ``(new_doc_text, changed)``. When ``check=True`` the file is
    not written — the caller compares the on-disk text against the
    returned text to decide pass / fail.
    """
    target = doc_path or DEFAULT_DOC_PATH
    original = target.read_text(encoding="utf-8")
    blocks = build_blocks(bibframe_path=bibframe_path, lkd_path=lkd_path)

    new_text = _replace_block(
        original,
        CLASSES_BEGIN_MARKER,
        CLASSES_END_MARKER,
        blocks.classes_block,
    )
    new_text = _replace_block(
        new_text,
        PREDICATES_BEGIN_MARKER,
        PREDICATES_END_MARKER,
        blocks.predicates_block,
    )

    changed = new_text != original
    if changed and not check:
        target.write_text(new_text, encoding="utf-8")
    return new_text, changed

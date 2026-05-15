"""M9 canonical-graph mutation + provenance emission + canonical-map loader.

Each successful reconciliation:

- adds ``<work> bffi:creator <auth>`` (creators) or
  ``<work> bffi:subject|genreForm <auth>`` (subjects) on the canonical Work
  (:func:`_apply_canonical_link`),
- bridges the original blank-node target with ``prov:specializationOf``,
- bumps the AdminMetadata block's ``sourceConsulted`` /
  ``descriptionChangeDate`` (:func:`_bump_admin_metadata`), flipping
  ``descriptionAuthentication`` to ``needs-review`` on tier-3 fallback,
- logs a ``bffi-prov:ReconciliationActivity`` via
  :func:`_emit_provenance`.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib import Literal as RdfLiteral

from bffi_pipeline.provenance import logger as P
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m9.schemas import (
    STAGE_WATCHDOG_ABORTED,
    EntityRequest,
    ReconciliationOutcome,
)


def _admin_block_for(graph: Graph, work: URIRef) -> URIRef | None:
    for block in graph.objects(work, V.adminMetadata):
        if isinstance(block, URIRef):
            return block
    return None


def _bump_admin_metadata(
    graph: Graph,
    work_uri: str,
    *,
    chosen_uri: str,
    needs_review: bool,
    now: datetime,
) -> None:
    """Side-effect: add sourceConsulted + bump descriptionChangeDate.

    On the fallback path also flip ``descriptionAuthentication`` to
    ``<bib:auth/needs-review>``.
    """
    block = _admin_block_for(graph, URIRef(work_uri))
    if block is None:
        return
    graph.add((block, V.sourceConsulted, URIRef(chosen_uri)))
    # Replace any existing descriptionChangeDate with the reconciliation moment.
    for old in list(graph.objects(block, V.descriptionChangeDate)):
        graph.remove((block, V.descriptionChangeDate, old))
    graph.add(
        (
            block,
            V.descriptionChangeDate,
            Literal(now.isoformat(), datatype=V.XSD.dateTime),
        )
    )
    if needs_review:
        for old in list(graph.objects(block, V.descriptionAuthentication)):
            graph.remove((block, V.descriptionAuthentication, old))
        graph.add((block, V.descriptionAuthentication, V.AUTH_NEEDS_REVIEW))


def _link_canonical_creator(graph: Graph, work_uri: str, chosen_uri: str) -> None:
    """Add ``<work> bffi:creator <authority>`` and rewrite the agent URI on the contribution.

    Also adds ``prov:specializationOf`` from the existing agent URI to
    the chosen authority URI so downstream consumers of the M3 raw graph
    still have a one-hop bridge to the reconciled identity.
    """
    work = URIRef(work_uri)
    auth = URIRef(chosen_uri)
    graph.add((work, V.BFFI.creator, auth))
    from rdflib.namespace import RDF

    for contrib in graph.objects(work, V.BFFI.contribution):
        if V.BFFI.PrimaryContribution not in set(graph.objects(contrib, RDF.type)):
            continue
        for agent in list(graph.objects(contrib, V.BFFI.agent)):
            if isinstance(agent, URIRef) and str(agent) != chosen_uri:
                graph.add((agent, V.PROV.specializationOf, auth))


def _link_canonical_subject(
    graph: Graph,
    *,
    work_uri: str,
    chosen_uri: str,
    predicate_uri: str,
    literal: str,
) -> None:
    """Add ``<work> <predicate> <authority>`` and bridge the original blank node.

    The blank-node target M8 propagated onto the canonical Work stays in
    place (it preserves the cataloguer's literal for audit), and gains a
    ``prov:specializationOf`` triple pointing at the chosen authority.
    The same predicate (``bffi:subject`` or ``bffi:genreForm``) the M8
    propagation used is re-used here — the cataloguer's MARC tag, not
    the Finto vocabulary, decides which slot the authority binds into.
    """
    work = URIRef(work_uri)
    auth = URIRef(chosen_uri)
    predicate = URIRef(predicate_uri)
    graph.add((work, predicate, auth))
    for target in graph.objects(work, predicate):
        if isinstance(target, URIRef):
            continue
        # Bridge only the blank node whose label matches the input literal,
        # so two distinct cataloguer subjects on the same canonical (e.g.
        # "Tampere" and "Helsinki") don't accidentally share a bridge.
        for label in graph.objects(target, V.RDFS.label):
            if isinstance(label, RdfLiteral) and str(label) == literal:
                graph.add((target, V.PROV.specializationOf, auth))
                break


def _apply_canonical_link(graph: Graph, request: EntityRequest, chosen_uri: str) -> None:
    """Dispatch the per-kind binding logic on a successful reconciliation.

    The dispatch hinges on whether the request came from the *creator*
    walker (no ``predicate_uri`` set; reconciles MARC 100/700 agents
    on the canonical's primary contribution) or the *subject* walker
    (``predicate_uri`` set to ``bffi:subject`` / ``bffi:genreForm``;
    reconciles MARC 6XX subject-as-name + topical/place/genre fields).
    Same kind (``person`` / ``corporate_body``) routes through KANTO at
    tier-1 in both cases — the predicate decides whether the bound URI
    lands as ``bffi:creator`` or ``bffi:subject`` on the canonical.
    """
    if request.predicate_uri is None:
        # Creator-walker request — kind must be person or corporate_body.
        _link_canonical_creator(graph, request.work_uri, chosen_uri)
        return
    _link_canonical_subject(
        graph,
        work_uri=request.work_uri,
        chosen_uri=chosen_uri,
        predicate_uri=request.predicate_uri,
        literal=request.literal,
    )


def _load_canonical_bib_ids(path: Path) -> dict[str, list[str]]:
    """Read ``canonical-map.jsonl`` and build ``canonical_work_uri →
    [helmet_bib_id, …]`` for the P-31 Phase C target-review wiring.

    Returns an empty dict when the file isn't present (M9 run against
    a hand-crafted canonical.ttl without M8 having produced the
    sidecar). The target-review rows just carry empty
    ``member_bib_ids`` in that case — the cataloguer still has the
    canonical Work URI to drill into.
    """
    if not path.is_file():
        return {}
    out: dict[str, list[str]] = {}
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            row = json.loads(line)
            uri = row.get("canonical_work_uri")
            ids = row.get("helmet_bib_ids") or []
            if isinstance(uri, str) and isinstance(ids, list):
                out[uri] = [b for b in ids if isinstance(b, str)]
    return out


def _emit_provenance(
    writer_graph: Graph | None,
    *,
    outcome: ReconciliationOutcome,
    started_at: datetime,
    ended_at: datetime,
) -> URIRef | None:
    if writer_graph is None:
        return None
    # Watchdog-aborted outcomes are recorded as ``"watchdog-aborted"`` in
    # provenance (matching M6's contract). The ``outcome.stage`` field
    # stays ``STAGE_FALLBACK`` for canonical-graph purposes — the
    # binding *is* a fallback — but the provenance Activity distinguishes
    # "LLM said uncertain" (``reconciliation-fallback``) from "LLM never
    # answered in time" (``watchdog-aborted``).
    stage_literal: str = STAGE_WATCHDOG_ABORTED if outcome.was_watchdog_aborted else outcome.stage
    # P-10 Phase B: cache-hit outcomes carry the cached Activity URI so
    # the new Activity links back via ``prov:wasInfluencedBy``.
    was_influenced_by = (
        URIRef(outcome.cached_activity_uuid) if outcome.cached_activity_uuid else None
    )
    return P.log_reconciliation(
        writer_graph,
        work_uri=outcome.request.work_uri,
        input_literal=outcome.request.literal,
        source_vocabulary=(
            outcome.candidates[0].source_vocabulary if outcome.candidates else "none"
        ),
        stage=stage_literal,
        chosen_authority_uri=outcome.chosen_uri,
        candidates=[(c.uri, c.lexical_similarity) for c in outcome.candidates],
        confidence=outcome.confidence,
        rationale=outcome.rationale,
        started_at=started_at,
        ended_at=ended_at,
        was_influenced_by=was_influenced_by,
    )

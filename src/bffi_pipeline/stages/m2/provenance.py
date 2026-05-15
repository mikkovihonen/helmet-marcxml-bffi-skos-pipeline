"""M2 BIBFRAME-graph post-process: Helmet identifier injection +
provenance + AdminMetadata triples.

Each converted record's graph gets three additions before it's
written to disk:

- :func:`_add_helmet_identifier` — ``bf:identifiedBy`` blank-node
  carrying the Helmet bib_id and ``bf:source <helmet>``.
- :func:`_add_marc_conversion_activity` — one ``bffi-prov:MarcConversion``
  Activity per record, used by ``prov:wasGeneratedBy`` on both Work
  and Instance.
- :func:`_add_admin_metadata_block` — one ``bffi:AdminMetadata`` block
  per Work/Instance with the M2 initial-stamp fields per spec § 8.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bffi_pipeline import __version__ as PIPELINE_VERSION
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2.xslt import marc2bibframe2_version

_BASEURI: Final[str] = "http://urn.fi/URN:NBN:fi:bib:raw/"
_HELMET_RECORD_NS: Final[str] = "http://urn.fi/URN:NBN:fi:bib:helmet/"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _find_root_resources(g: Graph) -> tuple[URIRef, URIRef]:
    """Return ``(work_uri, instance_uri)`` from the XSLT-produced graph.

    marc2bibframe2 emits one ``bf:Work`` per record's main entry plus
    additional ``bf:Work`` resources for "contained" / "related" works
    referenced via MARC 700 ind2=2 (analytical added entry), 740
    (added entry — uncontrolled name), 776 (additional physical form),
    and similar. The contained Works come into the graph as the
    *object* of ``bf:associatedResource`` triples on the main Work or
    Instance; the main Work is the one whose only inbound reference is
    ``bf:instanceOf`` from its Instance.
    """
    all_works = [s for s in g.subjects(RDF.type, V.BF.Work) if isinstance(s, URIRef)]
    if not all_works:
        raise RuntimeError("XSLT output contains no bf:Work")
    contained: set[URIRef] = {
        o for _, _, o in g.triples((None, V.BF.associatedResource, None)) if isinstance(o, URIRef)
    }
    main_works = [w for w in all_works if w not in contained]
    if not main_works:
        raise RuntimeError(
            "XSLT output contains bf:Work resources but all of them are "
            "referenced as bf:associatedResource — no main Work identifiable"
        )
    if len(main_works) > 1:
        raise RuntimeError(
            f"XSLT output contains {len(main_works)} candidate main bf:Work "
            f"resources (out of {len(all_works)} total); expected exactly 1"
        )
    work = main_works[0]
    instances = [o for o in g.objects(work, V.BF.hasInstance) if isinstance(o, URIRef)]
    if not instances:
        raise RuntimeError("XSLT output contains no bf:Instance linked to the Work")
    # marc2bibframe2 sometimes emits multiple bf:Instance resources
    # attached to the same Work via bf:hasInstance — most commonly an
    # `#Instance856-NN` secondary from a MARC 856 (Electronic Location
    # and Access) field, alongside the main `#Instance`. Without a
    # deterministic tie-breaker, ``instances[0]`` depends on rdflib
    # iteration order and post_process can attach Helmet identifier +
    # AdminMetadata to the wrong Instance; the Boundary-2 shape then
    # fires on the un-stamped main Instance. Prefer the URI ending in
    # ``#Instance`` (the marc2bibframe2 convention for the main
    # Instance); fall back to the first one if the convention doesn't
    # hold (defensive — shouldn't happen on Helmet exports).
    main_instances = [i for i in instances if str(i).endswith("#Instance")]
    instance = main_instances[0] if main_instances else instances[0]
    return work, instance


def _add_helmet_identifier(g: Graph, target: URIRef, helmet_id: str) -> None:
    ident = BNode()
    g.add((target, V.BF.identifiedBy, ident))
    g.add((ident, RDF.type, V.BF.Local))
    g.add((ident, RDF.value, Literal(helmet_id)))
    g.add((ident, V.BF.source, V.HELMET_SOURCE_URI))


def _add_marc_conversion_activity(
    g: Graph,
    *,
    work: URIRef,
    instance: URIRef,
    helmet_id: str,
    source_file: Path,
    converted_at: str,
) -> URIRef:
    activity = V.BIB[f"activity/marc-conv/{uuid.uuid4()}"]
    g.add((activity, RDF.type, V.PROV.Activity))
    g.add((activity, RDF.type, V.MarcConversion))
    g.add((activity, V.PROV.startedAtTime, Literal(converted_at, datatype=XSD.dateTime)))
    g.add((activity, V.PROV.endedAtTime, Literal(converted_at, datatype=XSD.dateTime)))
    g.add((activity, V.PROV.wasAssociatedWith, V.AGENT_MARC2BIBFRAME2))
    g.add((activity, V.PROV.used, URIRef(source_file.resolve().as_uri())))
    g.add((activity, V.helmetBibId, Literal(helmet_id)))
    g.add((activity, V.converterVersion, Literal(marc2bibframe2_version())))
    g.add((work, V.PROV.wasGeneratedBy, activity))
    g.add((instance, V.PROV.wasGeneratedBy, activity))
    return activity


def _admin_metadata_uri(target: URIRef, helmet_id: str) -> URIRef:
    suffix = "raw-work" if target.endswith("#Work") else "raw-instance"
    return V.BIB[f"adminmeta/{suffix}/{helmet_id}"]


def _add_admin_metadata_block(
    g: Graph,
    *,
    target: URIRef,
    helmet_id: str,
    activity: URIRef,
    converted_at: str,
) -> URIRef:
    am = _admin_metadata_uri(target, helmet_id)
    timestamp = Literal(converted_at, datatype=XSD.dateTime)
    helmet_record = URIRef(f"{_HELMET_RECORD_NS}{helmet_id}")
    pipeline_version_uri = V.BIB[f"gen-process/bffi-pipeline/v{PIPELINE_VERSION}"]

    g.add((target, V.adminMetadata, am))
    g.add((am, RDF.type, V.AdminMetadata))
    g.add((am, V.adminMetadataFor, target))
    g.add((am, V.descriptionCreationDate, timestamp))
    g.add((am, V.dateGenerated, timestamp))
    g.add((am, V.descriptionModifier, V.AGENT_MARC2BIBFRAME2))
    g.add((am, V.generationProcess, pipeline_version_uri))
    g.add((am, V.descriptionConventions, V.DESC_CONV_BFFI_1_0_0))
    g.add((am, V.descriptionLevel, V.DESC_LEVEL_MINIMUM))
    g.add((am, V.encodingLevel, V.ENC_LEVEL_AUTO))
    g.add((am, V.descriptionAuthentication, V.AUTH_AUTO_MERGED))
    g.add((am, V.recordingSource, V.RECORDING_SOURCE_HELMET))
    g.add((am, V.metadataLicensor, V.METADATA_LICENSOR_CC0))
    g.add((am, V.sourceMetadata, helmet_record))
    g.add((am, V.PROV.wasGeneratedBy, activity))
    return am

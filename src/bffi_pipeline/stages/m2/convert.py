"""M2 per-record conversion pipeline.

Glues the M2 sub-modules together for one input file:

1. Validate the MARCXML (Boundary 1).
2. Apply pre-XSLT byte-level repairs (``marcxml_repair``).
3. Run marc2bibframe2 via the cached XSLT.
4. Parse to an rdflib Graph.
5. ``post_process`` injects Helmet identifier + provenance Activity +
   AdminMetadata blocks (``provenance``).
6. SHACL-validate the BIBFRAME (Boundary 2).
7. Serialise to ``<output_dir>/bibframe/<helmet_id>.rdf`` atomically.

Returns a :class:`HelmetMapRow` for the success path or a typed
exception for the caller (``run()``) to route to ``_errors.jsonl``.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the multi-record driver loop. No logic change — moves only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from lxml import etree
from rdflib import Graph, URIRef
from rdflib.namespace import RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m2.marcxml_repair import (
    _sanitize_language_tags,
    _sanitize_subfield_separators,
)
from bffi_pipeline.stages.m2.provenance import (
    _BASEURI,
    _add_admin_metadata_block,
    _add_helmet_identifier,
    _add_marc_conversion_activity,
    _find_root_resources,
    _utc_now,
)
from bffi_pipeline.stages.m2.schemas import HelmetMapRow
from bffi_pipeline.stages.m2.sidecars import _atomic_write_bytes
from bffi_pipeline.stages.m2.xslt import _xslt, marc2bibframe2_version
from bffi_pipeline.validation.bibframe import assert_conforms
from bffi_pipeline.validation.marcxml import validate


def _run_xslt(tree: etree._ElementTree, helmet_id: str) -> etree._ElementTree:
    """Run marc2bibframe2 on ``tree`` and return the resulting RDF/XML tree."""
    result = _xslt()(
        tree,
        baseuri=etree.XSLT.strparam(_BASEURI),
        idfield=etree.XSLT.strparam("001"),
    )
    if result is None:
        raise RuntimeError(f"XSLT produced no output for {helmet_id}")
    return result


def _parse_to_graph(rdf_xml: bytes) -> Graph:
    g = Graph()
    g.parse(data=rdf_xml, format="xml")
    return g


def post_process(
    g: Graph,
    *,
    helmet_id: str,
    source_file: Path,
    converted_at: str | None = None,
) -> tuple[URIRef, URIRef]:
    """Add Helmet identifiers, conversion provenance, and AdminMetadata blocks.

    Returns ``(work_uri, instance_uri)`` for the side-effect graph. Mutates
    ``g`` in place.
    """
    converted_at = converted_at or _utc_now()
    work, instance = _find_root_resources(g)
    _add_helmet_identifier(g, work, helmet_id)
    _add_helmet_identifier(g, instance, helmet_id)
    activity = _add_marc_conversion_activity(
        g,
        work=work,
        instance=instance,
        helmet_id=helmet_id,
        source_file=source_file,
        converted_at=converted_at,
    )
    _add_admin_metadata_block(
        g,
        target=work,
        helmet_id=helmet_id,
        activity=activity,
        converted_at=converted_at,
    )
    _add_admin_metadata_block(
        g,
        target=instance,
        helmet_id=helmet_id,
        activity=activity,
        converted_at=converted_at,
    )
    g.bind("bf", V.BF)
    g.bind("bffi", V.BFFI)
    g.bind("bffi-prov", V.BFFI_PROV)
    g.bind("bib", V.BIB)
    g.bind("prov", V.PROV)
    g.bind("rdfs", RDFS)
    return work, instance


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _output_path_for(output_dir: Path, helmet_id: str) -> Path:
    return output_dir / "bibframe" / f"{helmet_id}.rdf"


def _iter_xml_files(input_dir: Path) -> Iterator[Path]:
    yield from sorted(input_dir.glob("*.xml"))


def _convert_one(
    input_path: Path,
    output_dir: Path,
    *,
    force: bool,
) -> tuple[HelmetMapRow | None, str]:
    """Convert one record. Returns ``(map_row, status)`` where status is one of
    ``"ok"``, ``"skipped"``; raises typed errors on failure.

    The caller catches errors and routes them to ``_errors.jsonl``.
    """
    validated = validate(input_path)
    helmet_id = validated.helmet_bib_id
    out = _output_path_for(output_dir, helmet_id)
    if not force and _is_output_fresh(input_path, out):
        return None, "skipped"

    converted_at = _utc_now()
    # Recover ``‡<code>``-separator copy-paste before the XSLT sees it:
    # cataloguers sometimes paste from a legacy ILS display that uses
    # ``‡`` as a visible subfield boundary, producing
    # ``<subfield code="a">value‡2slm/fin‡0http://...</subfield>``.
    # The split puts the right $2 / $0 / etc. content back under
    # proper subfield codes so marc2bibframe2 emits a proper
    # bf:source + cataloguer-supplied $0 URI binding.
    _sanitize_subfield_separators(validated.tree)
    rdf_tree = _run_xslt(validated.tree, helmet_id)
    # Repair invalid BCP-47 ``xml:lang`` attributes the XSLT
    # occasionally emits (``ru-``, ``uk-``) before handing them to
    # rdflib's parser, which would otherwise raise ``ValueError``.
    _sanitize_language_tags(rdf_tree)
    rdf_bytes = etree.tostring(rdf_tree, xml_declaration=True, encoding="utf-8")
    g = _parse_to_graph(rdf_bytes)
    work, instance = post_process(
        g,
        helmet_id=helmet_id,
        source_file=input_path,
        converted_at=converted_at,
    )
    assert_conforms(g, source_path=input_path)

    out.parent.mkdir(parents=True, exist_ok=True)
    serialised = g.serialize(format="pretty-xml").encode("utf-8")
    _atomic_write_bytes(out, serialised)

    return (
        HelmetMapRow(
            helmet_bib_id=helmet_id,
            source_file=input_path.name,
            raw_work_uri=str(work),
            raw_instance_uri=str(instance),
            converted_at=converted_at,
            marc2bibframe2_version=marc2bibframe2_version(),
        ),
        "ok",
    )

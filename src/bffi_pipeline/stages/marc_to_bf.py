"""Stage M2: MARCXML to BIBFRAME via the LoC ``marc2bibframe2`` XSLT.

For each input file the stage:

1. Validates the filename, encoding, XML syntax, and minimum content
   (Boundary 1 — :mod:`bffi_pipeline.validation.marcxml`).
2. Runs the XSLT to produce raw BIBFRAME RDF/XML, parameterising
   ``baseuri`` so the Work and Instance URIs carry the Helmet bib ID.
3. Post-processes the graph:
   - injects ``bf:identifiedBy`` triples linking every ``bf:Work`` and
     ``bf:Instance`` to the Helmet ``bf:Source`` URI, with the bare
     numeric bib ID as ``rdf:value``;
   - emits a ``bffi-prov:MarcConversion`` Activity (``prov:used`` =
     source filename, ``bffi-prov:helmetBibId``, ``bffi-prov:converterVersion``);
   - links the Work and Instance to the Activity via ``prov:wasGeneratedBy``;
   - mints one ``bffi:AdminMetadata`` block per Work/Instance, populated
     with the M2 initial-stamp fields (see stage M2 and
     spec § 8).
4. Validates the result against ``config/shapes/bibframe-conversion.shape.ttl``
   (Boundary 2 — :mod:`bffi_pipeline.validation.bibframe`).
5. Writes ``<output_dir>/bibframe/<helmet_id>.rdf`` atomically.

A row is appended to ``<output_dir>/helmet-map.jsonl`` for every success
and to ``<output_dir>/bibframe/_errors.jsonl`` for every typed failure.
Re-runs skip files whose output already exists and is newer than the
input; pass ``force=True`` to re-convert.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

from lxml import etree
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from bffi_pipeline import __version__ as PIPELINE_VERSION
from bffi_pipeline.cataloguer_review import append_source_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.observability import emit_if_active, get_active_emitter
from bffi_pipeline.validation.bibframe import BibframeShapeError, assert_conforms
from bffi_pipeline.validation.marcxml import MarcXmlValidationError, validate

_BASEURI: Final[str] = "http://urn.fi/URN:NBN:fi:bib:raw/"
_HELMET_RECORD_NS: Final[str] = "http://urn.fi/URN:NBN:fi:bib:helmet/"

#: P-11 Phase A progress cadence for M2 marc-to-bf.
_M2_PROGRESS_CADENCE: Final[int] = 100
_BFFI_PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_MARC2BIBFRAME2_DIR: Final[Path] = _BFFI_PIPELINE_REPO_ROOT / "third_party" / "marc2bibframe2"
_XSLT_PATH: Final[Path] = _MARC2BIBFRAME2_DIR / "xsl" / "marc2bibframe2.xsl"


# --- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class HelmetMapRow:
    """One row of ``helmet-map.jsonl`` per converted record."""

    helmet_bib_id: str
    source_file: str
    raw_work_uri: str
    raw_instance_uri: str
    converted_at: str
    marc2bibframe2_version: str


@dataclass(frozen=True)
class ConversionErrorRow:
    """One row of ``_errors.jsonl`` per failed record.

    ``run_uuid`` is populated from the active observability emitter
    so the exporter's error-tail loop (P-12 Option B) can attribute
    each row to its originating pipeline invocation. Empty string
    when no emitter is active (e.g. unit tests that bypass the CLI
    bootstrap) — rows surface under ``run_uuid=""`` in metrics.
    """

    helmet_bib_id: str | None
    filename: str
    error_type: str
    message: str
    run_uuid: str = ""


@dataclass
class ConversionSummary:
    """Aggregate counts for an end-of-run report."""

    succeeded: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    failed: list[ConversionErrorRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of input files seen across all outcomes."""
        return len(self.succeeded) + len(self.skipped_idempotent) + len(self.failed)

    def render(self) -> str:
        """Format this summary as paste-ready text for the marc-to-bf CLI."""
        lines = [
            f"MARCXML to BIBFRAME conversion summary ({self.total} input file(s))",
            f"  succeeded: {len(self.succeeded)}",
            f"  skipped (already converted): {len(self.skipped_idempotent)}",
            f"  failed: {len(self.failed)}",
        ]
        if self.failed:
            lines.append("Failures:")
            lines.extend(
                f"  - {row.filename}: [{row.error_type}] {row.message}" for row in self.failed
            )
        return "\n".join(lines)


# --- Caching --------------------------------------------------------------


@lru_cache(maxsize=1)
def _xslt() -> etree.XSLT:
    if not _XSLT_PATH.exists():
        raise RuntimeError(
            f"marc2bibframe2 XSLT not found at {_XSLT_PATH}. "
            "Run `git submodule update --init --recursive`."
        )
    return etree.XSLT(etree.parse(str(_XSLT_PATH)))


@lru_cache(maxsize=1)
def marc2bibframe2_version() -> str:
    """Return the marc2bibframe2 commit SHA (and tag if reachable)."""
    if not _MARC2BIBFRAME2_DIR.exists():
        return "unknown"
    try:
        sha = subprocess.run(
            ["git", "-C", str(_MARC2BIBFRAME2_DIR), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    try:
        tag = subprocess.run(
            ["git", "-C", str(_MARC2BIBFRAME2_DIR), "describe", "--tags", "--always"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return f"{tag}+{sha[:12]}"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return sha


# --- Conversion primitives ------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


#: MARCXML element/attribute names. lxml expanded form keeps the
#: comparison cheap and namespace-correct under
#: ``http://www.loc.gov/MARC21/slim``.
_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_SUBFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}subfield"

#: Regex for the cataloguer-pasted subfield separator. ``‡`` (U+2021,
#: DOUBLE DAGGER) was used as a visible separator in some legacy ILS
#: displays — operators sometimes copy-paste from those displays into
#: a single ``$a`` value, producing strings like
#: ``"taidemusiikki‡2slm/fin‡0http://urn.fi/URN:NBN:fi:au:slm:s474"``.
#: The capture group is the MARC subfield code (single alphanumeric);
#: the alternation excludes bare ``‡`` (e.g. a footnote dagger inside
#: a title) so we don't split legitimate uses.
_TAGGED_DAGGER_RE: Final[re.Pattern[str]] = re.compile(r"‡([0-9a-z])")

#: Length of the ``re.split`` result that has at least one capture
#: group fired — ``[leading_text, code1, content1]``.
_MIN_SPLIT_PARTS: Final[int] = 3

#: ``xml:lang`` attribute in expanded form (the XML namespace, not the
#: RDF/XML one). Walked over the XSLT output to repair malformed
#: BCP-47 tags before rdflib parses them — see
#: :func:`_sanitize_language_tags`.
_XML_LANG_ATTR: Final[str] = "{http://www.w3.org/XML/1998/namespace}lang"

#: One or more trailing ``-`` characters; the marc2bibframe2 XSLT
#: emits these (``ru-``, ``uk-``) when it can't map a MARC 008
#: country code into a BCP-47 region subtag.
_TRAILING_DASH_RE: Final[re.Pattern[str]] = re.compile(r"-+$")


def _sanitize_subfield_separators(tree: etree._ElementTree) -> int:
    """Split cataloguer-pasted ``‡<code>`` separators into proper
    MARCXML subfields, in place.

    Walks every ``<marc:subfield>`` element; for each value containing
    ``‡`` followed by a MARC subfield code (a-z / 0-9), splits the
    text at every such marker and rewrites the parent ``<datafield>``
    so the recovered subfield codes / values appear as proper
    sibling ``<subfield code="N">value</subfield>`` elements. The
    original subfield is kept in place (with its truncated leading
    value); the new subfields are inserted right after it, preserving
    cataloguer-intended order.

    Returns the count of *original* subfields rewritten, for operator
    visibility. The marc2bibframe2 XSLT then processes the corrected
    tree as if the cataloguer had typed the subfields correctly —
    proper ``bf:source`` and ``$0`` URI handling fall out automatically.
    """
    fixed = 0
    for subfield in tree.iter(_SUBFIELD_TAG):
        text = subfield.text or ""
        if "‡" not in text:
            continue
        parts = _TAGGED_DAGGER_RE.split(text)
        # re.split with one capture group yields:
        #   [leading_text, code1, content1, code2, content2, ...].
        # If no markers matched, parts == [text] — leave alone.
        if len(parts) < _MIN_SPLIT_PARTS:
            continue
        leading, *pairs = parts[0], *parts[1:]
        subfield.text = leading
        parent = subfield.getparent()
        if parent is None:
            continue
        insertion_index = list(parent).index(subfield) + 1
        # pairs alternates (code, content); build sibling subfields.
        for i in range(0, len(pairs), 2):
            code = pairs[i]
            content = pairs[i + 1] if i + 1 < len(pairs) else ""
            new_sf = etree.SubElement(parent, _SUBFIELD_TAG)
            new_sf.set("code", code)
            new_sf.text = content
            # SubElement appends; move into the right position.
            parent.remove(new_sf)
            parent.insert(insertion_index, new_sf)
            insertion_index += 1
        fixed += 1
    return fixed


def _sanitize_language_tags(tree: etree._ElementTree) -> int:
    """Strip trailing ``-`` from ``xml:lang`` attribute values, in place.

    marc2bibframe2 occasionally synthesises BCP-47 tags of the form
    ``<lang>-`` (e.g. ``ru-``, ``uk-``) when it tries to combine a MARC
    008 publication-country code with the language code and the region
    lookup falls through (the country is present in 008 positions
    15-17 but not in the converter's MARC-country → BCP-47-region
    lookup table). The trailing-hyphen tag is **invalid BCP-47** and
    rdflib's RDF/XML parser raises ``ValueError`` on it, killing the
    whole conversion before any other recovery can fire.

    This sanitiser trims any trailing hyphen run on every ``xml:lang``
    so ``ru-`` becomes ``ru`` — a valid bare-language tag.  Discovered
    in the P-02 5k production-style run (7 of 5000 records, all
    Russian / Ukrainian sources, hit this).

    Returns the count of attributes rewritten.
    """
    fixed = 0
    for el in tree.iter():
        lang = el.get(_XML_LANG_ATTR)
        if lang is None:
            continue
        repaired = _TRAILING_DASH_RE.sub("", lang)
        if repaired != lang:
            el.set(_XML_LANG_ATTR, repaired)
            fixed += 1
    return fixed


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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --- Driver ---------------------------------------------------------------


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


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_source_review_m2(row: ConversionErrorRow) -> None:
    """Mirror a ``ConversionErrorRow`` into the unified source-review TSV.

    Pairs with each ``_append_jsonl(errors_path, …)`` call so the
    cataloguer-handoff superset stays in lock-step with the per-stage
    `_errors.jsonl`. P-31 Phase B wire-in for M2.
    """
    append_source_row(
        bib_id=row.helmet_bib_id or row.filename,
        stage="m2",
        severity="blocking",
        details=row.message,
    )


def _emit_errors_tsv(path: Path, errors: list[ConversionErrorRow]) -> None:
    """Cataloguer-facing TSV companion to ``bibframe/_errors.jsonl``.

    Three columns the cataloguer can open in Excel / Sheets / Numbers
    and act on without parsing JSON:

    - ``helmet_bib_id`` — derived from the source filename's stem so
      it's populated even when the XSD parse failed before we could
      extract the 001 control field (the JSONL leaves
      ``helmet_bib_id=null`` in that case).
    - ``error_type`` — one of ``marcxml-xsd-validation``,
      ``marcxml-content-minimum``, ``bibframe-shape``,
      ``bibframe-conversion``. Filterable.
    - ``message`` — single-line, tab + newline + control char
      sanitised; truncated to keep the spreadsheet readable.

    Always emitted — even on a clean run a header-only TSV is
    written. Workflows wired to the artifact path don't need a
    missing-file guard.

    Sorted by (``helmet_bib_id``, ``error_type``) for stable diffs
    across re-runs. Atomic write via ``.tmp`` + ``replace``.
    """
    header = "helmet_bib_id\terror_type\tmessage\n"
    rows: list[tuple[str, str, str]] = []
    for row in errors:
        bib_id = row.helmet_bib_id or Path(row.filename).stem
        message_clean = " ".join(row.message.replace("\t", " ").split())
        if len(message_clean) > _ERRORS_TSV_MESSAGE_MAX:
            message_clean = message_clean[: _ERRORS_TSV_MESSAGE_MAX - 1] + "…"
        rows.append((bib_id, row.error_type, message_clean))
    rows.sort()
    body = "".join(f"{bib_id}\t{etype}\t{msg}\n" for bib_id, etype, msg in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)


#: Truncate over-long error messages in the TSV so a spreadsheet
#: stays readable. XSD validation errors carry the offending value
#: and a regex pattern — easily 400+ chars. The full message lives
#: in the JSONL for forensic lookup; the TSV is for triage.
_ERRORS_TSV_MESSAGE_MAX: Final[int] = 240


def _dedupe_helmet_map(path: Path) -> None:
    """Last-write-wins dedup on ``helmet_bib_id``. Rewrites atomically."""
    if not path.exists():
        return
    seen: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        seen[row["helmet_bib_id"]] = row
    rewritten = "\n".join(json.dumps(row, ensure_ascii=False) for row in seen.values()) + "\n"
    _atomic_write_bytes(path, rewritten.encode("utf-8"))


def run(
    input_dir: Path,
    *,
    output_dir: Path | None = None,
    force: bool = False,
) -> ConversionSummary:
    """Convert every ``*.xml`` file in ``input_dir`` and return a summary."""
    output_dir = output_dir or get_settings().data_dir
    summary = ConversionSummary()
    helmet_map_path = output_dir / "helmet-map.jsonl"
    errors_path = output_dir / "bibframe" / "_errors.jsonl"

    # P-12 Option B: include the active run_uuid on every error row so
    # the exporter's error-tail loop can attribute each typed failure
    # to its originating pipeline invocation. Empty when no emitter is
    # active (e.g. tests).
    emitter = get_active_emitter()
    run_uuid = emitter.run_uuid if emitter is not None else ""

    xml_files = list(_iter_xml_files(input_dir))
    emit_if_active(
        stage="m2",
        event="start",
        counters={"total": len(xml_files)},
    )

    for i, xml_path in enumerate(xml_files, start=1):
        if i % _M2_PROGRESS_CADENCE == 0:
            emit_if_active(
                stage="m2",
                event="progress",
                counters={"processed": i, "total": len(xml_files)},
                extra={
                    "succeeded": len(summary.succeeded),
                    "skipped": len(summary.skipped_idempotent),
                    "failed": len(summary.failed),
                },
            )
        try:
            map_row, status = _convert_one(xml_path, output_dir, force=force)
        except MarcXmlValidationError as exc:
            row = ConversionErrorRow(
                helmet_bib_id=xml_path.stem if xml_path.stem.isdigit() else None,
                filename=xml_path.name,
                error_type=exc.error_type,
                message=exc.message,
                run_uuid=run_uuid,
            )
            summary.failed.append(row)
            _append_jsonl(errors_path, asdict(row))
            _append_source_review_m2(row)
            continue
        except BibframeShapeError as exc:
            row = ConversionErrorRow(
                helmet_bib_id=xml_path.stem if xml_path.stem.isdigit() else None,
                filename=xml_path.name,
                error_type="bibframe-shape",
                message=exc.message,
                run_uuid=run_uuid,
            )
            summary.failed.append(row)
            _append_jsonl(
                errors_path,
                {**asdict(row), "report_text": exc.report_text},
            )
            _append_source_review_m2(row)
            continue
        except Exception as exc:
            row = ConversionErrorRow(
                helmet_bib_id=xml_path.stem if xml_path.stem.isdigit() else None,
                filename=xml_path.name,
                error_type="bibframe-conversion",
                message=str(exc),
                run_uuid=run_uuid,
            )
            summary.failed.append(row)
            _append_jsonl(errors_path, asdict(row))
            _append_source_review_m2(row)
            continue

        if status == "skipped":
            summary.skipped_idempotent.append(xml_path.name)
            continue

        assert map_row is not None  # status=='ok' implies a map row
        summary.succeeded.append(xml_path.name)
        _append_jsonl(helmet_map_path, asdict(map_row))

    _dedupe_helmet_map(helmet_map_path)
    # Cataloguer-facing TSV companion to ``bibframe/_errors.jsonl``.
    # Always written (even header-only on a clean run) so cataloguer
    # workflows wired to the artifact path don't need a missing-file
    # guard. Mirrors the M8 mint-failures TSV pattern.
    errors_tsv_path = output_dir / "bibframe" / "_errors.tsv"
    _emit_errors_tsv(errors_tsv_path, summary.failed)
    emit_if_active(
        stage="m2",
        event="end",
        counters={
            "total": len(xml_files),
            "succeeded": len(summary.succeeded),
            "skipped": len(summary.skipped_idempotent),
            "failed": len(summary.failed),
        },
    )
    return summary


__all__ = [
    "ConversionErrorRow",
    "ConversionSummary",
    "HelmetMapRow",
    "marc2bibframe2_version",
    "post_process",
    "run",
]

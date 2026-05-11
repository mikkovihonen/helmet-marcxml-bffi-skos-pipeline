"""Boundary 1: MARCXML pre-conversion validation.

The four typed-error families (see ``docs/archived/BUILD_PLAN.md`` M2) :

* ``marcxml-filename`` — filename does not match ``^\\d+\\.xml$``.
* ``marcxml-encoding`` — file is not strict UTF-8.
* ``marcxml-xml-syntax`` — file does not parse as XML.
* ``marcxml-xsd-validation`` — XML parses but does not validate against the
  vendored ``MARC21slim.xsd``.
* ``marcxml-content-minimum`` — XSD-valid but missing the minimum-content
  set the pipeline relies on (1XX/7XX, 245, 008, 336/337/338).

All checks raise :class:`MarcXmlValidationError` carrying the typed code; the
stage layer catches the exception and writes a row to ``_errors.jsonl``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from lxml import etree

from bffi_pipeline.schemas import marc21slim_xsd_path

MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.xml$")

ErrorType = Literal[
    "marcxml-filename",
    "marcxml-encoding",
    "marcxml-xml-syntax",
    "marcxml-xsd-validation",
    "marcxml-content-minimum",
]


class MarcXmlValidationError(Exception):
    """Typed Boundary-1 failure. ``error_type`` drives the ``_errors.jsonl`` row."""

    def __init__(self, *, error_type: ErrorType, message: str, path: Path) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.path = path

    def __str__(self) -> str:
        return f"[{self.error_type}] {self.path.name}: {self.message}"


@dataclass(frozen=True)
class ValidatedMarcXml:
    """Successful Boundary-1 outcome — the parsed tree and the bib ID."""

    helmet_bib_id: str
    tree: etree._ElementTree


def helmet_bib_id_from_filename(path: Path) -> str:
    """Return the numeric bib ID from a filename matching ``^\\d+\\.xml$``.

    Caller has already checked the pattern via :func:`validate_filename`.
    """
    return path.stem


def validate_filename(path: Path) -> None:
    """Raise if ``path.name`` does not match ``^\\d+\\.xml$``."""
    if not _FILENAME_PATTERN.match(path.name):
        raise MarcXmlValidationError(
            error_type="marcxml-filename",
            message="Filename does not match ^\\d+\\.xml$",
            path=path,
        )


def validate_utf8(path: Path) -> bytes:
    """Read ``path`` as strict UTF-8; raise on any decoding error.

    Returns the decoded text re-encoded as UTF-8 bytes so downstream lxml
    parsing sees clean bytes rather than a possibly-mis-encoded blob.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MarcXmlValidationError(
            error_type="marcxml-encoding",
            message=f"Not strict UTF-8: {exc}",
            path=path,
        ) from exc
    return text.encode("utf-8")


def parse_xml(path: Path, raw: bytes) -> etree._ElementTree:
    """Parse ``raw`` as XML; raise on syntax errors."""
    try:
        return etree.ElementTree(etree.fromstring(raw))
    except etree.XMLSyntaxError as exc:
        raise MarcXmlValidationError(
            error_type="marcxml-xml-syntax",
            message=f"XML syntax error: {exc}",
            path=path,
        ) from exc


@lru_cache(maxsize=1)
def _xsd_validator() -> etree.XMLSchema:
    """Cached ``lxml.etree.XMLSchema`` for the vendored ``MARC21slim.xsd``."""
    return etree.XMLSchema(etree.parse(str(marc21slim_xsd_path())))


def validate_xsd(path: Path, tree: etree._ElementTree) -> None:
    """Validate ``tree`` against the cached MARC21slim schema."""
    validator = _xsd_validator()
    if not validator.validate(tree):
        message = str(validator.error_log)
        raise MarcXmlValidationError(
            error_type="marcxml-xsd-validation",
            message=f"XSD validation failed: {message}",
            path=path,
        )


def _has_field(record: etree._Element, tag_pattern: re.Pattern[str]) -> bool:
    for df in record.iterfind(f".//{{{MARC_NS}}}datafield"):
        tag = df.get("tag")
        if tag and tag_pattern.match(tag):
            return True
    for cf in record.iterfind(f".//{{{MARC_NS}}}controlfield"):
        tag = cf.get("tag")
        if tag and tag_pattern.match(tag):
            return True
    return False


_RE_1XX_OR_7XX: Final[re.Pattern[str]] = re.compile(r"^(1\d\d|7\d\d)$")
_RE_245: Final[re.Pattern[str]] = re.compile(r"^245$")
_RE_008: Final[re.Pattern[str]] = re.compile(r"^008$")
_RE_336_337_338: Final[re.Pattern[str]] = re.compile(r"^33[678]$")

#: MARC leader position 6 codes for which the 33X RDA content/media/
#: carrier triplet is commonly absent in Helmet's Sierra export. The
#: leader's record-type-code already conveys the broad type, so we
#: don't hard-skip these records over a missing 33X.
#: - ``c``: Notated music
#: - ``d``: Manuscript notated music
#: - ``i``: Nonmusical sound recording
#: - ``j``: Musical sound recording
_LEADER_TYPES_WITHOUT_33X: Final[frozenset[str]] = frozenset({"c", "d", "i", "j"})


#: Position of the record-type-code in the MARC leader (0-indexed).
_LEADER_RECORD_TYPE_POS: Final[int] = 6


def _leader_record_type(record: etree._Element) -> str | None:
    """Return the MARC leader's record-type-code (position 6), or ``None``."""
    leader_el = record.find(f"{{{MARC_NS}}}leader")
    if leader_el is None or leader_el.text is None:
        return None
    leader = leader_el.text
    if len(leader) <= _LEADER_RECORD_TYPE_POS:
        return None
    return leader[_LEADER_RECORD_TYPE_POS]


def validate_minimum_content(path: Path, tree: etree._ElementTree) -> None:
    """Require ≥1 1XX/7XX, ≥1 245, ≥1 008, and (for non-music records) ≥1 336/337/338."""
    record = tree.getroot()
    # Records may be wrapped in <collection>; if so, take the first record.
    if record.tag == f"{{{MARC_NS}}}collection":
        first = record.find(f"{{{MARC_NS}}}record")
        if first is None:
            raise MarcXmlValidationError(
                error_type="marcxml-content-minimum",
                message="<collection> contains no <record>",
                path=path,
            )
        record = first

    record_type = _leader_record_type(record)
    requires_33x = record_type not in _LEADER_TYPES_WITHOUT_33X

    missing = []
    if not _has_field(record, _RE_1XX_OR_7XX):
        missing.append("1XX/7XX (creator)")
    if not _has_field(record, _RE_245):
        missing.append("245 (title)")
    if not _has_field(record, _RE_008):
        missing.append("008 (fixed-length data)")
    if requires_33x and not _has_field(record, _RE_336_337_338):
        missing.append("336/337/338 (content/media/carrier type)")
    if missing:
        raise MarcXmlValidationError(
            error_type="marcxml-content-minimum",
            message="Missing required MARC fields: " + ", ".join(missing),
            path=path,
        )


def validate(path: Path) -> ValidatedMarcXml:
    """Run all five Boundary-1 checks in order; return parsed tree on success."""
    validate_filename(path)
    raw = validate_utf8(path)
    tree = parse_xml(path, raw)
    validate_xsd(path, tree)
    validate_minimum_content(path, tree)
    return ValidatedMarcXml(helmet_bib_id=helmet_bib_id_from_filename(path), tree=tree)


__all__ = [
    "MARC_NS",
    "ErrorType",
    "MarcXmlValidationError",
    "ValidatedMarcXml",
    "helmet_bib_id_from_filename",
    "parse_xml",
    "validate",
    "validate_filename",
    "validate_minimum_content",
    "validate_utf8",
    "validate_xsd",
]

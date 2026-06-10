"""Per-record MARCXML diff classification.

Compares one source MARCXML record (the original Helmet output) against
one reconstructed MARCXML record (the step-4 reverse converter's output)
and yields a per-field-instance verdict.

v0 statuses:

  - ``identical``  — same tag, same indicators, same subfields / value.
  - ``changed``    — same tag (and same instance index among repeated tags),
                     content differs.
  - ``lost``       — present in source, no counterpart in reconstructed.
  - ``added``      — present in reconstructed, no counterpart in source.

Deferred to a follow-on:

  - ``tag-changed`` — same content under a different tag (e.g. MARC 260
    → 264; marc2bibframe2 collapses both source forms into the same
    ``bf:ProvisionActivity`` shape, so the reverse converter emits the
    RDA-modern 264 ind2=1 form for either source). Needs cross-tag
    content similarity; for v0 the operator reads paired ``lost`` +
    ``added`` rows as a hint instead.
  - ``marckey-bypass`` — concept lifted from P-49 on main; doesn't apply
    until the reverse converter starts reading ``bflc:marcKey``.

The pairing algorithm for repeated tags (e.g. multiple 700s on one
record) is intentionally simple: exact-content matches in either order
are claimed first as ``identical``; remaining source/reconstructed
pairs at the same tag are zipped positionally as ``changed``; any
overhang on either side falls out as ``lost`` / ``added``. Step 6's
discriminator routings make the rows more comparable; the diff
algorithm doesn't need to be smart about that yet.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from lxml import etree

DiffStatus = Literal["identical", "changed", "lost", "added"]

#: MARCXML namespace per the LoC slim schema.
MARC21_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_MARC = f"{{{MARC21_NS}}}"


@dataclass(frozen=True)
class FieldRow:
    """One MARC field instance normalised for equality comparison."""

    tag: str
    #: ``None`` for controlfields (001-009); two-char string for datafields.
    ind1: str | None
    ind2: str | None
    #: Empty tuple for controlfields; for datafields the ordered list of
    #: ``(subfield_code, subfield_text)`` pairs.
    subfields: tuple[tuple[str, str], ...]
    #: For controlfields: the bare text value. ``None`` for datafields.
    text: str | None

    def display(self) -> str:
        """Pretty-print one field for the cataloguer-review HTML."""
        if self.text is not None:
            return f"{self.tag}  {self.text}"
        subs = "".join(f" ${code}{value}" for code, value in self.subfields)
        ind1 = self.ind1 if self.ind1 is not None else "_"
        ind2 = self.ind2 if self.ind2 is not None else "_"
        return f"{self.tag} {ind1}{ind2}{subs}"


@dataclass(frozen=True)
class FieldDiff:
    """One row of the per-record diff."""

    tag: str
    status: DiffStatus
    source: FieldRow | None
    reconstructed: FieldRow | None


@dataclass(frozen=True)
class RecordDiff:
    """Diff outcome for one (source, reconstructed) MARCXML pair."""

    bib_id: str
    fields: tuple[FieldDiff, ...]

    @property
    def status_counts(self) -> dict[DiffStatus, int]:
        counts: Counter[DiffStatus] = Counter()
        for field in self.fields:
            counts[field.status] += 1
        return dict(counts)


# --- parsing -------------------------------------------------------------


class MarcxmlParseError(RuntimeError):
    """The MARCXML file couldn't be parsed or didn't contain a record."""


def parse_record(marcxml_path: Path) -> tuple[str, tuple[FieldRow, ...]]:
    """Read one MARCXML file. Return ``(bib_id, fields)``.

    Accepts either ``<record>`` as the document root or
    ``<collection><record/></collection>``; the first ``<record>`` wins.
    Bib ID = ``controlfield tag="001"`` text. Raises
    :exc:`MarcxmlParseError` if either is missing.
    """
    try:
        tree = etree.parse(str(marcxml_path))
    except etree.XMLSyntaxError as exc:
        raise MarcxmlParseError(f"xml parse failed for {marcxml_path}: {exc}") from exc

    root = tree.getroot()
    record = root if root.tag == f"{_MARC}record" else root.find(f"{_MARC}record")
    if record is None:
        raise MarcxmlParseError(f"no <record> element in {marcxml_path}")

    bib_id_el = record.find(f"{_MARC}controlfield[@tag='001']")
    if bib_id_el is None or not bib_id_el.text:
        raise MarcxmlParseError(f"no controlfield 001 in {marcxml_path}")

    fields: list[FieldRow] = []
    for child in record:
        if child.tag == f"{_MARC}controlfield":
            tag = child.get("tag")
            if tag is None:
                continue
            fields.append(
                FieldRow(
                    tag=tag,
                    ind1=None,
                    ind2=None,
                    subfields=(),
                    text=child.text or "",
                )
            )
        elif child.tag == f"{_MARC}datafield":
            tag = child.get("tag")
            if tag is None:
                continue
            subfields: list[tuple[str, str]] = []
            for sf in child.findall(f"{_MARC}subfield"):
                code = sf.get("code") or ""
                subfields.append((code, sf.text or ""))
            fields.append(
                FieldRow(
                    tag=tag,
                    ind1=child.get("ind1") or "",
                    ind2=child.get("ind2") or "",
                    subfields=tuple(subfields),
                    text=None,
                )
            )

    return bib_id_el.text, tuple(fields)


# --- diff ----------------------------------------------------------------


def diff_fields(
    *, source: tuple[FieldRow, ...], reconstructed: tuple[FieldRow, ...]
) -> tuple[FieldDiff, ...]:
    """Per-field-instance diff between source and reconstructed records."""
    by_tag_source: dict[str, list[FieldRow]] = {}
    by_tag_recon: dict[str, list[FieldRow]] = {}
    for row in source:
        by_tag_source.setdefault(row.tag, []).append(row)
    for row in reconstructed:
        by_tag_recon.setdefault(row.tag, []).append(row)

    diffs: list[FieldDiff] = []
    all_tags = sorted(set(by_tag_source) | set(by_tag_recon))

    for tag in all_tags:
        source_rows = list(by_tag_source.get(tag, ()))
        recon_rows = list(by_tag_recon.get(tag, ()))

        # Pass 1: pair exact-match instances as `identical`.
        survivors_s: list[FieldRow] = []
        for s in source_rows:
            try:
                idx = recon_rows.index(s)
            except ValueError:
                survivors_s.append(s)
                continue
            del recon_rows[idx]
            diffs.append(FieldDiff(tag=tag, status="identical", source=s, reconstructed=s))

        # Pass 2: zip positionally — `changed` for the overlapping zone.
        paired_count = min(len(survivors_s), len(recon_rows))
        for s, r in zip(survivors_s[:paired_count], recon_rows[:paired_count], strict=True):
            diffs.append(FieldDiff(tag=tag, status="changed", source=s, reconstructed=r))

        # Pass 3: overhang.
        for s in survivors_s[paired_count:]:
            diffs.append(FieldDiff(tag=tag, status="lost", source=s, reconstructed=None))
        for r in recon_rows[paired_count:]:
            diffs.append(FieldDiff(tag=tag, status="added", source=None, reconstructed=r))

    return tuple(diffs)


def diff_records(*, source_path: Path, reconstructed_path: Path) -> RecordDiff:
    """Top-level entry: parse both files, diff them, return a :class:`RecordDiff`."""
    bib_id, source_fields = parse_record(source_path)
    _, recon_fields = parse_record(reconstructed_path)
    return RecordDiff(
        bib_id=bib_id,
        fields=diff_fields(source=source_fields, reconstructed=recon_fields),
    )

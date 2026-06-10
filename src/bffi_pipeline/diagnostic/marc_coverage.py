"""Coverage diagnostic for the BFFI → MARC reverse converter.

Walks a directory of source MARCXML records and quantifies how much of
the corpus the registry in
:data:`bffi_pipeline.stages.bffi_to_marc.runner.MARC_EMIT_REGISTRY`
covers. Two complementary metrics:

  - **Field coverage** — ratio of source field rows (``<leader>`` +
    ``<controlfield>`` + ``<datafield>``) whose MARC tag is in the
    registry. A coarse signal: does the converter emit *anything* for
    this row?
  - **Subfield coverage** — ratio of source ``<subfield>`` occurrences
    whose ``(parent_tag, code)`` pair is in the registry. A finer
    signal: when we cover a tag, do we cover every subfield the
    cataloguer wrote?

The diagnostic does not consult the BFFI graph or run the converter —
it compares the source MARC structure against the registry declarations
only. The registry is the single source of truth for what the
converter promises to emit; mismatches against the source are the
backlog.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from lxml import etree

from bffi_pipeline.stages.bffi_to_marc.runner import MARC_EMIT_REGISTRY, MarcEmitMeta

#: MARCXML namespace per the LoC MARC21 slim schema.
MARCXML_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_MARC: Final[str] = f"{{{MARCXML_NS}}}"


@dataclass(frozen=True)
class TagStat:
    """Per-tag occurrence + coverage breakdown."""

    tag: str
    occurrences: int
    covered: bool
    subfield_total: int
    subfield_covered: int
    #: Codes the source carries under this tag that the registry does not
    #: emit, mapped to the number of times each appeared.
    uncovered_subfield_codes: dict[str, int]


@dataclass
class CoverageReport:
    """Aggregate corpus-coverage statistics."""

    records: int = 0
    total_fields: int = 0
    covered_fields: int = 0
    total_subfields: int = 0
    covered_subfields: int = 0
    per_tag: dict[str, TagStat] = field(default_factory=dict)

    @property
    def field_coverage(self) -> float:
        """Fraction of source field rows whose tag is in the registry."""
        return self.covered_fields / self.total_fields if self.total_fields else 0.0

    @property
    def subfield_coverage(self) -> float:
        """Fraction of source ``<subfield>`` occurrences whose
        ``(tag, code)`` pair is in the registry."""
        return self.covered_subfields / self.total_subfields if self.total_subfields else 0.0


@dataclass(frozen=True)
class _RegistryView:
    """Pre-computed lookups derived from :data:`MARC_EMIT_REGISTRY`.

    Built once at the start of corpus analysis so each record's lookups
    are O(1) hash hits rather than O(n) scans of the registry list.
    """

    tags: frozenset[str]
    subfield_codes_by_tag: dict[str, frozenset[str]]

    @classmethod
    def from_registry(cls, registry: list[MarcEmitMeta]) -> _RegistryView:
        tags: set[str] = set()
        codes: dict[str, set[str]] = {}
        for entry in registry:
            tags.add(entry.tag)
            codes.setdefault(entry.tag, set()).update(code for code, _ in entry.subfields)
        return cls(
            tags=frozenset(tags),
            subfield_codes_by_tag={tag: frozenset(cs) for tag, cs in codes.items()},
        )


def analyse_corpus(input_dir: Path) -> CoverageReport:
    """Walk every ``*.xml`` MARCXML file under ``input_dir`` and return
    the coverage report.

    Treats each ``<record>`` element in each file as one record (so a
    multi-record collection file counts as multiple records). The
    ``<leader>`` pseudo-tag is counted once per record.
    """
    view = _RegistryView.from_registry(MARC_EMIT_REGISTRY)
    report = CoverageReport()
    tag_occurrences: Counter[str] = Counter()
    tag_subfield_total: Counter[str] = Counter()
    tag_subfield_covered: Counter[str] = Counter()
    tag_uncovered_codes: dict[str, Counter[str]] = {}

    for xml_path in sorted(input_dir.glob("*.xml")):
        tree = etree.parse(str(xml_path))
        root = tree.getroot()
        for record in root.iter(f"{_MARC}record"):
            report.records += 1
            _accumulate_record(
                record,
                view,
                report,
                tag_occurrences,
                tag_subfield_total,
                tag_subfield_covered,
                tag_uncovered_codes,
            )

    for tag, count in tag_occurrences.items():
        report.per_tag[tag] = TagStat(
            tag=tag,
            occurrences=count,
            covered=tag in view.tags,
            subfield_total=tag_subfield_total[tag],
            subfield_covered=tag_subfield_covered[tag],
            uncovered_subfield_codes=dict(tag_uncovered_codes.get(tag, Counter())),
        )
    return report


def _accumulate_record(
    record: etree._Element,
    view: _RegistryView,
    report: CoverageReport,
    tag_occurrences: Counter[str],
    tag_subfield_total: Counter[str],
    tag_subfield_covered: Counter[str],
    tag_uncovered_codes: dict[str, Counter[str]],
) -> None:
    """Tally one ``<record>``'s field and subfield occurrences into ``report``."""
    leader = record.find(f"{_MARC}leader")
    if leader is not None:
        report.total_fields += 1
        tag_occurrences["leader"] += 1
        if "leader" in view.tags:
            report.covered_fields += 1

    for cf in record.iter(f"{_MARC}controlfield"):
        tag = cf.get("tag", "")
        report.total_fields += 1
        tag_occurrences[tag] += 1
        if tag in view.tags:
            report.covered_fields += 1

    for df in record.iter(f"{_MARC}datafield"):
        tag = df.get("tag", "")
        report.total_fields += 1
        tag_occurrences[tag] += 1
        tag_covered = tag in view.tags
        if tag_covered:
            report.covered_fields += 1
        covered_codes = view.subfield_codes_by_tag.get(tag, frozenset())
        for sf in df.iter(f"{_MARC}subfield"):
            code = sf.get("code", "")
            report.total_subfields += 1
            tag_subfield_total[tag] += 1
            if code in covered_codes:
                report.covered_subfields += 1
                tag_subfield_covered[tag] += 1
            else:
                tag_uncovered_codes.setdefault(tag, Counter())[code] += 1


def format_report(report: CoverageReport, *, top_n: int = 20) -> str:
    """Render the coverage report as a human-readable text block.

    Lists, in order:
      1. Headline field- and subfield-coverage percentages.
      2. Top ``top_n`` tags sorted by source-occurrence count, showing
         per-tag coverage and the most-frequent uncovered subfields.
      3. Top 10 uncovered tags by impact (occurrences * % of corpus
         field rows) — the high-value follow-on backlog.
    """
    lines: list[str] = []
    lines.append(f"Records analysed: {report.records:,}")
    lines.append("")
    lines.append("Field coverage (leader + controlfield + datafield rows):")
    lines.append(
        f"  total {report.total_fields:>10,}   "
        f"covered {report.covered_fields:>10,}   "
        f"({report.field_coverage * 100:.1f}%)"
    )
    lines.append("Subfield coverage (subfield occurrences):")
    lines.append(
        f"  total {report.total_subfields:>10,}   "
        f"covered {report.covered_subfields:>10,}   "
        f"({report.subfield_coverage * 100:.1f}%)"
    )
    lines.append("")
    lines.append(f"Top {top_n} tags by source occurrence:")
    lines.append(_format_per_tag_header())
    sorted_tags = sorted(report.per_tag.values(), key=lambda s: (-s.occurrences, s.tag))
    for stat in sorted_tags[:top_n]:
        lines.append(_format_per_tag_row(stat))

    lines.append("")
    lines.append("Top uncovered tags (highest source impact):")
    uncovered = sorted(
        (s for s in report.per_tag.values() if not s.covered),
        key=lambda s: (-s.occurrences, s.tag),
    )
    if not uncovered:
        lines.append("  (none — every tag in the corpus is in the registry)")
    else:
        for stat in uncovered[:10]:
            pct = stat.occurrences / report.total_fields * 100 if report.total_fields else 0.0
            lines.append(
                f"  {stat.tag:<7} {stat.occurrences:>8,} occurrences  ({pct:.1f}% of corpus rows)"
            )
    return "\n".join(lines)


def _format_per_tag_header() -> str:
    return (
        f"  {'tag':<7}  {'occ':>8}  cov  "
        f"{'sf_tot':>7} {'sf_cov':>7}  {'sf_%':>5}  uncovered codes (top 5)"
    )


def _format_per_tag_row(stat: TagStat) -> str:
    cov_mark = "Y" if stat.covered else "."
    if stat.subfield_total > 0:
        sf_pct = f"{stat.subfield_covered / stat.subfield_total * 100:.0f}%"
    else:
        sf_pct = "—"
    codes_sorted = sorted(stat.uncovered_subfield_codes.items(), key=lambda kv: (-kv[1], kv[0]))
    codes_str = ", ".join(f"${c}({n})" for c, n in codes_sorted[:5]) or "—"
    return (
        f"  {stat.tag:<7}  {stat.occurrences:>8,}  {cov_mark:^3}  "
        f"{stat.subfield_total:>7} {stat.subfield_covered:>7}  "
        f"{sf_pct:>5}  {codes_str}"
    )

"""Round-trip subfield-level diff analyser.

Scans every (source MARCXML, reconstructed MARCXML) pair in a run and
classifies the differences into a small set of recurring issue types:

- ``row_dropped`` — source datafield with no matching recon row
- ``row_added`` — recon datafield with no matching source row
- ``tag_misrouted`` — paired rows but source/recon tags differ
  (e.g. source 648 emitted as 650)
- ``ind1_changed`` / ``ind2_changed`` — indicator value differs
- ``subfield_missing_<code>`` — source row carries ``$<code>`` that the
  paired recon row doesn't
- ``subfield_added_<code>`` — recon row carries ``$<code>`` that the
  paired source row doesn't (excluding pipeline-internal ``$5``/``$9``)
- ``subfield_<code>_concatenated_into_<target>`` — recon row's
  ``$<target>`` value contains the (punctuation-stripped) text of the
  source row's ``$<code>``, AND recon lacks ``$<code>``. Catches the
  730 ``$l`` → ``$a`` and ``$o`` → ``$a`` merges.
- ``trailing_punct_stripped_<code>`` — paired ``$<code>`` values are
  byte-identical modulo a trailing ISBD-punct character
  (`` ,.;:/``) on source

Pairing strategy
----------------
Each recon row carries either ``$9 src=<bib>:<source-tag>:<ord>``
(P-50 Phase A) or ``$9 src=<source-tag>-<rank>`` (legacy). The first
form gives an exact source-row match; the second falls back to
matching by ``(tag, rank)`` within the record. Recon rows without
``$9 src=…`` are listed as ``row_added`` (Phase G.bis 700 ind2=2
synthesised entries, M9 LoC-vocab labels, etc.).

Output
------
A Markdown report under ``scratchpad/`` with three tables:
1. Issue prevalence (issue type → record / occurrence counts).
2. Per-tag breakdown (tag → counts by issue category).
3. Most-affected records (top 20 by total issue count).

Usage
-----
    uv run python scripts/roundtrip-diff-analysis.py \\
        --source-dir marcxml/samples/helmet/500/marcxml/ \\
        --recon-dir  runs/20260609-0330-74bd43/marc-roundtrip/reconstructed/ \\
        --out        scratchpad/2026-06-09-roundtrip-diff.md
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MARC_NS: Final[str] = "{http://www.loc.gov/MARC21/slim}"
PIPELINE_SUBFIELD_CODES: Final[frozenset[str]] = frozenset({"5", "9"})
ISBD_PUNCT_TAIL: Final[str] = " ,.;:/"

# Regexes for parsing the lineage tokens our round-trip emits in $9.
_LINEAGE_NEW = re.compile(r"^src=(?P<bib>[^:]+):(?P<tag>\d{3}):(?P<ord>\d+)$")
_LINEAGE_LEGACY = re.compile(r"^src=(?P<tag>\d{3})-(?P<rank>\d+)$")


@dataclass(frozen=True)
class SourceRowKey:
    bib_id: str
    tag: str
    ordinal: int  # 1-indexed within (bib_id, tag)


@dataclass
class IssueCounts:
    """Per-record-tag-issue counters; aggregated at report time."""

    issue_records: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    issue_occurrences: Counter[str] = field(default_factory=Counter)
    issues_by_tag: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    record_issue_count: Counter[str] = field(default_factory=Counter)
    tag_misrouting_samples: dict[tuple[str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    concatenation_samples: dict[tuple[str, str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def add(self, *, bib_id: str, tag: str, issue: str) -> None:
        self.issue_records[issue].add(bib_id)
        self.issue_occurrences[issue] += 1
        self.issues_by_tag[tag][issue] += 1
        self.record_issue_count[bib_id] += 1


def parse_lineage(df: ET.Element) -> tuple[str, str, int | None, int | None] | None:
    """Return ``(bib_id_or_blank, source_tag, ord, legacy_rank)`` from
    the recon row's ``$9 src=…``. Bib id is empty string for the legacy
    form. ``ord`` is set for the new form; ``legacy_rank`` for the old.
    """
    for sf in df.findall(f"{MARC_NS}subfield"):
        if sf.get("code") != "9":
            continue
        text = sf.text or ""
        m = _LINEAGE_NEW.match(text)
        if m:
            return (m.group("bib"), m.group("tag"), int(m.group("ord")), None)
        m = _LINEAGE_LEGACY.match(text)
        if m:
            return ("", m.group("tag"), None, int(m.group("rank")))
    return None


def has_marckey_bypass_marker(df: ET.Element) -> bool:
    """Return True if the recon datafield carries a ``$9 marckey-bypass``
    marker — the converter's flag for "this row's subfields came from
    bflc:marcKey, not from a structured BFFI predicate." Added by
    ``_emit_datafield`` whenever ``marckey_bypass=True`` is passed.
    """
    for sf in df.findall(f"{MARC_NS}subfield"):
        if sf.get("code") == "9" and (sf.text or "") == "marckey-bypass":
            return True
    return False


def subfield_items(df: ET.Element, *, drop_pipeline: bool = True) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for sf in df.findall(f"{MARC_NS}subfield"):
        code = sf.get("code") or ""
        if drop_pipeline and code in PIPELINE_SUBFIELD_CODES:
            continue
        out.append((code, (sf.text or "")))
    return out


def index_recon_rows(
    recon_root: ET.Element, bib_id: str
) -> tuple[dict[SourceRowKey, ET.Element], dict[tuple[str, int], ET.Element], list[ET.Element]]:
    """Return three views over the recon datafields:

    - by exact source-row key (``$9 src=<bib>:<tag>:<ord>``)
    - by ``(source_tag, legacy_rank)`` for the older lineage format
    - the list of rows with NO lineage (synthesised / M9-rebound)
    """
    by_source_key: dict[SourceRowKey, ET.Element] = {}
    by_legacy_rank: dict[tuple[str, int], ET.Element] = {}
    no_lineage: list[ET.Element] = []
    for df in recon_root.findall(f"{MARC_NS}datafield"):
        parsed = parse_lineage(df)
        if parsed is None:
            no_lineage.append(df)
            continue
        lineage_bib, source_tag, ord_, legacy_rank = parsed
        if ord_ is not None:
            key = SourceRowKey(bib_id=(lineage_bib or bib_id), tag=source_tag, ordinal=ord_)
            by_source_key[key] = df
        elif legacy_rank is not None:
            by_legacy_rank[(source_tag, legacy_rank)] = df
    return by_source_key, by_legacy_rank, no_lineage


def numerate_source_rows(source_root: ET.Element) -> dict[SourceRowKey, ET.Element]:
    by_key: dict[SourceRowKey, ET.Element] = {}
    counts: Counter[tuple[str, str]] = Counter()
    bib_id_from_root = source_root.findtext(f"{MARC_NS}controlfield[@tag='001']") or ""
    for df in source_root.findall(f"{MARC_NS}datafield"):
        tag = df.get("tag") or ""
        counts[(bib_id_from_root, tag)] += 1
        key = SourceRowKey(
            bib_id=bib_id_from_root, tag=tag, ordinal=counts[(bib_id_from_root, tag)]
        )
        by_key[key] = df
    return by_key


def diff_pair(src: ET.Element, recon: ET.Element, counts: IssueCounts, bib_id: str) -> None:
    src_tag = src.get("tag") or ""
    recon_tag = recon.get("tag") or ""
    src_ind1, src_ind2 = src.get("ind1") or " ", src.get("ind2") or " "
    recon_ind1, recon_ind2 = recon.get("ind1") or " ", recon.get("ind2") or " "

    if has_marckey_bypass_marker(recon):
        counts.add(bib_id=bib_id, tag=recon_tag, issue="marckey_bypass")

    if src_tag != recon_tag:
        counts.add(bib_id=bib_id, tag=src_tag, issue="tag_misrouted")
        key = (src_tag, recon_tag)
        if len(counts.tag_misrouting_samples[key]) < 3:
            counts.tag_misrouting_samples[key].append(bib_id)

    if src_ind1 != recon_ind1:
        counts.add(bib_id=bib_id, tag=src_tag, issue="ind1_changed")
    if src_ind2 != recon_ind2:
        counts.add(bib_id=bib_id, tag=src_tag, issue="ind2_changed")

    src_subs = subfield_items(src)
    recon_subs = subfield_items(recon)
    src_codes = {c for c, _ in src_subs}
    recon_codes = {c for c, _ in recon_subs}

    for missing in src_codes - recon_codes:
        # Try to detect concatenation BEFORE counting as plain "missing".
        src_val = next(v for c, v in src_subs if c == missing)
        concat_target = _find_concat_target(missing, src_val, recon_subs)
        if concat_target is not None:
            issue = f"subfield_{missing}_concatenated_into_{concat_target}"
            counts.add(bib_id=bib_id, tag=src_tag, issue=issue)
            key = (src_tag, missing, concat_target)
            if len(counts.concatenation_samples[key]) < 3:
                counts.concatenation_samples[key].append(bib_id)
        else:
            counts.add(bib_id=bib_id, tag=src_tag, issue=f"subfield_missing_{missing}")

    for added in recon_codes - src_codes:
        counts.add(bib_id=bib_id, tag=src_tag, issue=f"subfield_added_{added}")

    # Trailing-punct check — compare paired same-code values.
    for code in src_codes & recon_codes:
        src_vals = [v for c, v in src_subs if c == code]
        recon_vals = [v for c, v in recon_subs if c == code]
        for sv, rv in zip(src_vals, recon_vals):
            if sv == rv:
                continue
            if sv.rstrip(ISBD_PUNCT_TAIL) == rv.rstrip(ISBD_PUNCT_TAIL) and sv != rv:
                # One side lost trailing punctuation.
                if len(sv) > len(rv):
                    counts.add(
                        bib_id=bib_id,
                        tag=src_tag,
                        issue=f"trailing_punct_stripped_{code}",
                    )
                else:
                    counts.add(
                        bib_id=bib_id,
                        tag=src_tag,
                        issue=f"trailing_punct_added_{code}",
                    )


def _find_concat_target(
    missing_code: str, missing_val: str, recon_subs: list[tuple[str, str]]
) -> str | None:
    """If any recon subfield value contains the source value (after
    stripping ISBD punct), return that recon subfield's code. Empty
    or whitespace-only values never count as concat targets."""
    needle = missing_val.strip(ISBD_PUNCT_TAIL).strip()
    if not needle or len(needle) < 3:  # noqa: PLR2004 — avoid spurious 1-2 char matches
        return None
    for code, val in recon_subs:
        if needle in val:
            return code
    return None


def diff_record(src_path: Path, recon_path: Path, counts: IssueCounts) -> None:
    src_root = ET.parse(str(src_path)).getroot()
    recon_root = ET.parse(str(recon_path)).getroot()
    bib_id = src_root.findtext(f"{MARC_NS}controlfield[@tag='001']") or src_path.stem

    src_by_key = numerate_source_rows(src_root)
    by_source_key, by_legacy_rank, no_lineage = index_recon_rows(recon_root, bib_id)

    # Group everything by source-tag so we can do per-tag ordinal
    # pairing AFTER the lineage-based pairing exhausts.
    matched_recon: set[int] = set()
    legacy_rank_counts: Counter[str] = Counter()
    unmatched_source: dict[str, list[ET.Element]] = defaultdict(list)
    for key, src_df in src_by_key.items():
        legacy_rank_counts[key.tag] += 1
        recon_df = by_source_key.get(key) or by_legacy_rank.get(
            (key.tag, legacy_rank_counts[key.tag])
        )
        if recon_df is None:
            unmatched_source[key.tag].append(src_df)
            continue
        matched_recon.add(id(recon_df))
        diff_pair(src_df, recon_df, counts, bib_id)

    # Tag-bucketed ordinal pairing for the remaining source rows + the
    # lineage-less recon rows. Same-tag unmatched-source ↔ same-tag
    # no-lineage recon, in document order. Only emits row_dropped or
    # row_added_no_lineage when one side has more rows than the other.
    no_lineage_by_tag: dict[str, list[ET.Element]] = defaultdict(list)
    for df in no_lineage:
        no_lineage_by_tag[df.get("tag") or "?"].append(df)
    all_tags = set(unmatched_source) | set(no_lineage_by_tag)
    for tag in all_tags:
        src_list = unmatched_source.get(tag, [])
        recon_list = no_lineage_by_tag.get(tag, [])
        pair_n = min(len(src_list), len(recon_list))
        for i in range(pair_n):
            matched_recon.add(id(recon_list[i]))
            diff_pair(src_list[i], recon_list[i], counts, bib_id)
        for src_df in src_list[pair_n:]:
            counts.add(bib_id=bib_id, tag=tag, issue="row_dropped")
        for recon_df in recon_list[pair_n:]:
            counts.add(bib_id=bib_id, tag=tag, issue="row_added_no_lineage")
            if has_marckey_bypass_marker(recon_df):
                counts.add(bib_id=bib_id, tag=tag, issue="marckey_bypass")

    for df in by_source_key.values():
        if id(df) not in matched_recon:
            counts.add(
                bib_id=bib_id, tag=df.get("tag") or "?", issue="row_added_with_lineage"
            )
            if has_marckey_bypass_marker(df):
                counts.add(bib_id=bib_id, tag=df.get("tag") or "?", issue="marckey_bypass")
    for df in by_legacy_rank.values():
        if id(df) not in matched_recon:
            counts.add(
                bib_id=bib_id, tag=df.get("tag") or "?", issue="row_added_with_lineage"
            )
            if has_marckey_bypass_marker(df):
                counts.add(bib_id=bib_id, tag=df.get("tag") or "?", issue="marckey_bypass")


def render_report(counts: IssueCounts, n_records: int) -> str:
    lines: list[str] = []
    lines.append("# Round-trip subfield-level diff scan")
    lines.append("")
    lines.append(f"Records scanned: **{n_records}**")
    lines.append("")
    lines.append("## 1. Issue prevalence")
    lines.append("")
    lines.append("| Issue | Records | Occurrences |")
    lines.append("|---|--:|--:|")
    sorted_issues = sorted(
        counts.issue_occurrences.items(), key=lambda kv: (-kv[1], kv[0])
    )
    for issue, occ in sorted_issues:
        rec = len(counts.issue_records[issue])
        lines.append(f"| `{issue}` | {rec} | {occ} |")
    lines.append("")
    lines.append("## 2. Per-tag breakdown (top 30 tags by total issues)")
    lines.append("")
    tag_totals = {
        tag: sum(by_issue.values()) for tag, by_issue in counts.issues_by_tag.items()
    }
    top_tags = sorted(tag_totals.items(), key=lambda kv: -kv[1])[:30]
    lines.append("| Tag | Total | Issues breakdown |")
    lines.append("|---|--:|---|")
    for tag, total in top_tags:
        by_issue = counts.issues_by_tag[tag]
        breakdown = ", ".join(
            f"`{issue}`={cnt}" for issue, cnt in by_issue.most_common(8)
        )
        if len(by_issue) > 8:  # noqa: PLR2004
            breakdown += f", +{len(by_issue) - 8} more"
        lines.append(f"| {tag} | {total} | {breakdown} |")
    lines.append("")
    lines.append("## 3. Tag misrouting (top pairs)")
    lines.append("")
    lines.append("| Source tag | Recon tag | Records | Samples |")
    lines.append("|---|---|--:|---|")
    sorted_misrouting = sorted(
        counts.tag_misrouting_samples.items(),
        key=lambda kv: -counts.issues_by_tag[kv[0][0]].get("tag_misrouted", 0),
    )
    for (src, recon), samples in sorted_misrouting[:20]:
        # Count via issues_by_tag isn't precise per src→recon pair;
        # approximate by sample count.
        lines.append(
            f"| {src} | {recon} | {len(samples)}+ | {', '.join(samples[:3])} |"
        )
    lines.append("")
    lines.append("## 4. Subfield concatenations (top patterns)")
    lines.append("")
    lines.append("| Tag | Source subfield | Concatenated into | Records | Samples |")
    lines.append("|---|---|---|--:|---|")
    sorted_concat = sorted(
        counts.concatenation_samples.items(), key=lambda kv: -len(kv[1])
    )
    for (tag, src_sub, target), samples in sorted_concat[:30]:
        lines.append(
            f"| {tag} | `${src_sub}` | `${target}` | {len(samples)}+ | "
            f"{', '.join(samples[:3])} |"
        )
    lines.append("")
    lines.append("## 5. marcKey-bypass dependency (per-tag)")
    lines.append("")
    lines.append(
        "Rows that took at least one subfield value from ``bflc:marcKey`` "
        "rather than a structured BFFI / BIBFRAME predicate. Carries a "
        "``$9 marckey-bypass`` marker on the recon row + ``status: "
        '"marckey-bypass"`` in the per-record diff JSON. A high count for '
        "a tag flags either an upstream gap (marc2bibframe2 doesn't emit "
        "the predicate) or an M3 SPARQL gap (the predicate exists but "
        "isn't propagated)."
    )
    lines.append("")
    lines.append("| Tag | marcKey-bypass rows |")
    lines.append("|---|--:|")
    bypass_by_tag = sorted(
        (
            (tag, by_issue["marckey_bypass"])
            for tag, by_issue in counts.issues_by_tag.items()
            if by_issue.get("marckey_bypass")
        ),
        key=lambda t: -t[1],
    )
    for tag, n in bypass_by_tag:
        lines.append(f"| {tag} | {n} |")
    bypass_total = counts.issue_occurrences.get("marckey_bypass", 0)
    bypass_records = len(counts.issue_records.get("marckey_bypass", set()))
    lines.append("")
    lines.append(
        f"Total marcKey-bypass rows: **{bypass_total}** across **{bypass_records}** records."
    )
    lines.append("")
    lines.append("## 6. Most-affected records (top 20)")
    lines.append("")
    lines.append("| bib_id | Issues |")
    lines.append("|---|--:|")
    for bib_id, n in counts.record_issue_count.most_common(20):
        lines.append(f"| {bib_id} | {n} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source-dir", type=Path, required=True)
    ap.add_argument("--recon-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    counts = IssueCounts()
    n_records = 0
    for src_path in sorted(args.source_dir.glob("*.xml")):
        recon_path = args.recon_dir / src_path.name
        if not recon_path.is_file():
            continue
        try:
            diff_record(src_path, recon_path, counts)
            n_records += 1
        except ET.ParseError as exc:
            print(f"skip {src_path.name}: {exc}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(counts, n_records), encoding="utf-8")
    print(f"Wrote {args.out} ({n_records} records, {sum(counts.issue_occurrences.values())} issues)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

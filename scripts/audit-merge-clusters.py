"""Triage M5/M6 same_work merge clusters from a pipeline run.

Reads a ``merge-clusters.csv`` (SPARQL output from Fuseki listing
canonical Works with > 1 ``bf:identifiedBy`` Helmet bib ID) and for
each cluster pulls MARC 100 / 245 / 041 / 264 / 008 from the source
MARCXML files. Classifies each cluster via heuristic rules and writes
a verdict JSONL.

Verdict categories
------------------

- ``legitimate_translation`` — high-confidence cross-language same-Work
  merge. Same author, same core title (no volume markers), language
  varies, publication years within reason.
- ``legitimate_multilingual_publication`` — same publisher, same year,
  three distinct languages, same scope (gov't / museum / statistical
  publications).
- ``legitimate_reedition`` — same author, same title, same language,
  different printings/years.
- ``series_volumes_collapsed`` — same author + same series prefix +
  volume-number markers in titles, DIFFERENT volume numbers. M5/M6
  false positive class A (cf. prop-20 Schildt case).
- ``different_scope_same_canon`` — anthology vs single-work case
  (e.g. "Sherlock Holmes : complete novels" + a specific Holmes book).
  False-positive class B.
- ``subject_misread`` — MARC 100 person is the subject not the author
  (Aalto-style). Pattern: title prefix matches the 100 person + 245$c
  cites different actual authors. False-positive class C (cf. prop-21).
- ``uncertain`` — heuristics can't classify; cataloguer eye needed.

The classification is BIASED TOWARD ``uncertain`` over false-positives
to ``legitimate_*``. A misclassification as ``uncertain`` costs the
cataloguer a manual look; a misclassification as ``legitimate_*``
would hide a real bug.

Output: ``scratchpad/merge-cluster-verdicts/verdicts.jsonl`` (one row
per cluster) + ``summary.md`` (aggregate counts per category).

Usage:
    uv run python scripts/audit-merge-clusters.py \\
        --csv scratchpad/overnight-sample-2026-05-13/merge-clusters.csv \\
        --marcxml-dir marcxml/sierra \\
        --out-dir scratchpad/merge-cluster-verdicts
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final

# Volume markers in 245 titles. Order matters — match the longest first.
_VOLUME_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bVol\.\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bvolume\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bosa\s+(\d+)\b", re.IGNORECASE),  # Finnish "part N"
    re.compile(r"\.\s*(\d+)\s*(?::|$|\s*/)"),  # "Title. 3 :" or "Title. 3 /"
    re.compile(r"\b(\d{1,3})\s*[:/.]"),  # standalone number at title end
)

# Anthology markers — "complete", "selected", "kaikki", etc.
_ANTHOLOGY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bcomplete\b", re.IGNORECASE),
    re.compile(r"\bselected\b", re.IGNORECASE),
    re.compile(r"\bcollected\b", re.IGNORECASE),
    re.compile(r"\bkaikki\b", re.IGNORECASE),  # Finnish "all"
    re.compile(r"\bvalitut\b", re.IGNORECASE),  # Finnish "selected"
    re.compile(r"\bsamlade\b", re.IGNORECASE),  # Swedish "collected"
)

# Helmet 100-as-subject heuristic — known deceased Finnish cultural
# figures who routinely appear in 100 on monographs ABOUT them.
# Conservative list; extend as audit surfaces more cases.
_LIKELY_SUBJECT_PERSONS: Final[set[str]] = {
    "Aalto, Alvar",
    "Sibelius, Jean",
    "Mannerheim, Carl Gustaf Emil",
    "Mannerheim, Carl Gustaf",
    "Jansson, Tove",  # iff posthumous; she died 2001
    "Gallen-Kallela, Akseli",
    "Schjerfbeck, Helene",
    "Edelfelt, Albert",
    "Saarinen, Eero",
    "Saarinen, Eliel",
    "Eliel, Saarinen",
}


def _extract_marcxml_fields(path: Path) -> dict[str, Any]:
    """Pull MARC 100, 245, 041, 264 (or 260), 008 from one MARCXML file.

    Returns a dict with keys:
      - ``author``: 100$a (stripped of trailing comma)
      - ``author_id``: 100$0 (if present, FI-ASTERI-N-style)
      - ``author_role``: 100$e (relator label)
      - ``title_main``: 245$a stripped of trailing punctuation
      - ``title_sub``: 245$b stripped (None if not present)
      - ``title_resp``: 245$c (statement of responsibility)
      - ``lang``: 041$a (expression language)
      - ``lang_orig``: 041$h (original language) if present
      - ``year``: 264$c or 260$c (parsed to int when possible)
      - ``publisher``: 264$b or 260$b
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")

    def _datafield(tag: str) -> str | None:
        m = re.search(
            rf'<datafield ind1="[^"]*" ind2="[^"]*" tag="{tag}">(.*?)</datafield>',
            text,
        )
        return m.group(1) if m else None

    def _subfield(field: str | None, code: str) -> str | None:
        if field is None:
            return None
        m = re.search(rf'<subfield code="{code}">([^<]*)</subfield>', field)
        if not m:
            return None
        # HTML-decode the entities Helmet's MARCXML uses. ``html.unescape``
        # handles every named + numeric entity in one pass, so we don't
        # have to maintain a per-character table.
        import html as _html

        return _html.unescape(m.group(1))

    f100 = _datafield("100")
    f245 = _datafield("245")
    f041 = _datafield("041")
    f264 = _datafield("264") or _datafield("260")

    def _strip_marc_punct(s: str | None) -> str | None:
        if s is None:
            return None
        return s.rstrip(" /:,.;")

    year_raw = _subfield(f264, "c")
    year_int: int | None = None
    if year_raw:
        m = re.search(r"\b(\d{4})\b", year_raw)
        if m:
            year_int = int(m.group(1))

    return {
        "author": _strip_marc_punct(_subfield(f100, "a")),
        "author_id": _subfield(f100, "0"),
        "author_role": _strip_marc_punct(_subfield(f100, "e")),
        "title_main": _strip_marc_punct(_subfield(f245, "a")),
        "title_sub": _strip_marc_punct(_subfield(f245, "b")),
        "title_resp": _strip_marc_punct(_subfield(f245, "c")),
        "lang": _subfield(f041, "a"),
        "lang_orig": _subfield(f041, "h"),
        "year": year_int,
        "publisher": _strip_marc_punct(_subfield(f264, "b")),
    }


def _has_volume_marker(title: str) -> int | None:
    """Return the volume number if the title carries one; None otherwise."""
    for pat in _VOLUME_PATTERNS:
        m = pat.search(title)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _is_anthology_title(title: str) -> bool:
    return any(p.search(title) for p in _ANTHOLOGY_PATTERNS)


def _classify_cluster(
    cluster_marc: list[dict[str, Any]],
) -> tuple[str, str]:
    """Apply heuristics to one cluster's MARC records.

    Returns (verdict, rationale).
    """
    if not cluster_marc:
        return "uncertain", "No MARC data could be extracted."

    def _norm_author(a: str | None) -> str | None:
        """Case-fold and strip diacritics for set-deduplication purposes.

        Keeps the original form on the row's ``marc_authors`` field; this
        helper is just for the heuristic's "same-author" comparison so
        ``'Le Carré, John'`` and ``'Le Carre, John'`` (the same Helmet
        author entered with and without diacritics) count as one.
        """
        if not a:
            return None
        import unicodedata as _ud

        nfkd = _ud.normalize("NFKD", a)
        return "".join(c for c in nfkd if not _ud.combining(c)).casefold().strip(" ,.")

    raw_authors = {m.get("author") for m in cluster_marc if m.get("author")}
    authors = {_norm_author(a) for a in raw_authors}
    authors.discard(None)
    titles = [m.get("title_main") for m in cluster_marc]
    subtitles = [m.get("title_sub") for m in cluster_marc]
    langs = {m.get("lang") for m in cluster_marc if m.get("lang")}
    orig_langs = {m.get("lang_orig") for m in cluster_marc if m.get("lang_orig")}
    years = [m.get("year") for m in cluster_marc if m.get("year")]
    publishers = {m.get("publisher") for m in cluster_marc if m.get("publisher")}
    resps = [m.get("title_resp") or "" for m in cluster_marc]
    volume_nums = [_has_volume_marker(t or "") for t in titles]
    has_volumes = [v for v in volume_nums if v is not None]

    # --- Series volumes collapsed (false positive A) ---
    # Two or more records have volume numbers and the volume numbers differ.
    if len(has_volumes) >= 2 and len(set(has_volumes)) >= 2:
        return (
            "series_volumes_collapsed",
            f"Cluster contains different volumes of the same series. "
            f"Volume markers detected: {sorted(set(has_volumes))}. "
            f"Each volume is a distinct Work in FRBR semantics — should not merge.",
        )

    # --- Anthology vs. single-work (false positive B) ---
    # One title is an anthology marker ("complete", "selected"), others
    # are specific named works.
    anth_flags = [_is_anthology_title(t or "") for t in titles]
    if any(anth_flags) and not all(anth_flags):
        return (
            "different_scope_same_canon",
            f"One title is an anthology / collection (complete / selected / "
            f"collected), other titles are specific named works. Mixing anthology "
            f"and component works is a scope mismatch. Titles: {titles}.",
        )

    # --- Subject misread (false positive C / Aalto case) ---
    # 100 person matches a known-deceased + posthumous-popular figure
    # AND title prefix == that person's name AND 245$c cites different
    # actual authors.
    if len(authors) == 1:
        author = next(iter(authors))
        # ``_LIKELY_SUBJECT_PERSONS`` is original-case; ``author`` here
        # is the normalized (case-folded, diacritic-stripped) form, so
        # compare against the same-normalized whitelist.
        _norm_subjects = {_norm_author(p) for p in _LIKELY_SUBJECT_PERSONS}
        _norm_subjects.discard(None)
        if author in _norm_subjects:
            # Check if the title prefix matches the author name.
            person_lastname_first = author.split(",")[0].strip()
            person_firstname = (
                author.split(",", 1)[1].strip().split()[0] if "," in author else ""
            )
            title_prefix_match = any(
                person_lastname_first.lower() in (t or "").lower()
                or (
                    person_firstname
                    and person_firstname.lower() in (t or "").lower()
                )
                for t in titles
            )
            # Different actual authors in 245$c → strong subject-misread signal.
            distinct_resps = {r for r in resps if r}
            if title_prefix_match and len(distinct_resps) >= 2:
                return (
                    "subject_misread",
                    f"MARC 100 carries '{author}' (a known historical/cultural "
                    f"figure routinely placed in 100 as SUBJECT of art/architecture "
                    f"monographs, not as author). Title prefixes contain the person's "
                    f"name; statements of responsibility in 245$c cite different actual "
                    f"authors. This is the prop-21 100-as-subject false-positive class.",
                )

    # --- Subtitle divergence (prop-20 Schildt-on-Aalto class) ---
    # Same author + same main title + DIFFERENT subtitles. Even with
    # multiple languages, distinct subtitles within the same series
    # signal distinct Works (different subjects, different volumes
    # marketed under a series prefix). The Alvar Aalto / Schildt case
    # is the prototype: same main title "Alvar Aalto", subtitles
    # "mestariteoksia" vs "his life".
    distinct_main_titles = {(t or "").lower().rstrip(" :") for t in titles}
    distinct_subtitles = {(s or "").lower().rstrip(" :/") for s in subtitles if s}
    if (
        len(authors) == 1
        and len(distinct_main_titles) == 1
        and len(distinct_subtitles) >= 2
    ):
        return (
            "subtitle_divergence",
            f"Single author '{next(iter(raw_authors))}', identical main title "
            f"({next(iter(distinct_main_titles))!r}), but distinct subtitles: "
            f"{sorted(distinct_subtitles)}. Same-author + shared-main-title + "
            f"distinct-subtitles is the prop-20 Schildt-on-Aalto false-positive "
            f"class (different books in a series sharing only the prefix).",
        )

    # --- Legitimate translation candidate ---
    # Same author + multiple languages + similar titles (no volume markers).
    if len(authors) == 1 and len(langs) >= 2 and not has_volumes:
        # Translation pairs typically share lang_orig.
        if orig_langs:
            return (
                "legitimate_translation",
                f"Single author '{next(iter(raw_authors))}', titles in {sorted(langs)}, "
                f"original-language metadata present ({sorted(orig_langs)}). "
                f"Likely a legitimate cross-language same-Work merge.",
            )
        return (
            "legitimate_translation",
            f"Single author '{next(iter(raw_authors))}', titles in {sorted(langs)}. "
            f"No original-language link present but the cross-language pattern "
            f"suggests a same-Work merge.",
        )

    # --- Multilingual publication (gov / museum / stats) ---
    # Same publisher, same year, three+ distinct languages.
    if (
        len(publishers) == 1
        and len(set(years)) == 1
        and len(langs) >= 3
    ):
        return (
            "legitimate_multilingual_publication",
            f"Same publisher ({next(iter(publishers))}), same year "
            f"({years[0] if years else '?'}), {len(langs)} languages. "
            f"Pattern of government / statistical / museum publication.",
        )

    # --- Legitimate re-edition (same author, same title, different years) ---
    if (
        len(authors) == 1
        and len(set(t.lower() if t else "" for t in titles)) == 1
        and len(set(years)) >= 2
    ):
        return (
            "legitimate_reedition",
            f"Same author '{next(iter(raw_authors))}', identical title across "
            f"records, different publication years {sorted(set(years))}. "
            f"Different printings of the same Work — merge is correct.",
        )

    # --- Annual series (statistical yearbooks, road maps, etc.) ---
    # Same main title MODULO year/edition suffix + different years.
    # These are SUSPICIOUS — each year's edition is a distinct Work in
    # FRBR (different content year-over-year). Annual reports / yearbooks
    # / regularly-updated reference works should NOT merge year-over-year.
    def _strip_year(t: str | None) -> str:
        if not t:
            return ""
        # Drop trailing 4-digit year and any " = " (parallel title separator).
        return re.sub(r"\s*(\d{4})?\s*=?\s*$", "", t).strip().lower()

    stripped_titles = {_strip_year(t) for t in titles}
    # Only classify as annual_series if the year ALSO appears in the
    # original title text (e.g. "Yearbook 2019" vs "Yearbook 2020").
    # Otherwise re-editions of "A sign of affection" published in
    # 2021 and 2024 falsely qualify because the strip yields the same
    # stem on titles that never carried a year.
    _title_has_year = any(
        re.search(r"\b\d{4}\b", t or "") for t in titles
    )
    if (
        len(stripped_titles) == 1
        and stripped_titles != {""}
        and len(set(years)) >= 2
        and _title_has_year
    ):
        return (
            "annual_series_collapsed",
            f"Records share a main-title stem ({next(iter(stripped_titles))!r}) "
            f"but differ in publication year {sorted(set(years))}. Annual "
            f"editions / yearbooks / regularly-updated reference works are "
            f"distinct Works in FRBR; this merge is likely a false positive.",
        )

    # --- Different authors entirely ---
    if len(authors) >= 2:
        return (
            "uncertain",
            f"Cluster spans multiple distinct MARC-100 authors: "
            f"{sorted(raw_authors)}. Suspicious — would need cataloguer review.",
        )

    # --- Fallback ---
    return (
        "uncertain",
        f"Heuristics inconclusive. Titles: {titles}. Languages: {sorted(langs)}. "
        f"Years: {sorted(set(years)) if years else 'unknown'}.",
    )


def _process(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    marcxml_dir = Path(args.marcxml_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not csv_path.is_file():
        print(f"ERROR: --csv not found: {csv_path}", file=sys.stderr)
        return 2
    if not marcxml_dir.is_dir():
        print(f"ERROR: --marcxml-dir not a directory: {marcxml_dir}", file=sys.stderr)
        return 2

    verdicts_path = out_dir / "verdicts.jsonl"
    summary_path = out_dir / "summary.md"

    verdict_counter: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []

    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bib_ids = row["bibIds"].split(",")
            titles_combined = row["titles"]
            cluster_marc = [
                _extract_marcxml_fields(marcxml_dir / f"{bib}.xml") for bib in bib_ids
            ]
            verdict, rationale = _classify_cluster(cluster_marc)
            verdict_counter[verdict] += 1
            rows.append(
                {
                    "canonical_work_uri": row["work"],
                    "bib_ids": bib_ids,
                    "bib_count": int(row["bibCount"]),
                    "titles_from_fuseki": titles_combined,
                    "marc_authors": sorted(
                        {m.get("author") for m in cluster_marc if m.get("author")}
                    ),
                    "marc_titles": [m.get("title_main") for m in cluster_marc],
                    "marc_subtitles": [m.get("title_sub") for m in cluster_marc],
                    "marc_languages": sorted(
                        {m.get("lang") for m in cluster_marc if m.get("lang")}
                    ),
                    "marc_years": sorted(
                        {m.get("year") for m in cluster_marc if m.get("year")}
                    ),
                    "verdict": verdict,
                    "rationale": rationale,
                }
            )

    with verdicts_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Summary report — verdict counts + top 5 examples per category.
    lines = ["# Merge-cluster audit summary\n", "## Verdict distribution\n", "| Verdict | Count |", "|---|---:|"]
    for verdict, count in verdict_counter.most_common():
        lines.append(f"| `{verdict}` | {count} |")
    lines.append("\n## Examples per category\n")
    for verdict in verdict_counter:
        lines.append(f"### `{verdict}` ({verdict_counter[verdict]} total)\n")
        examples = [r for r in rows if r["verdict"] == verdict][:5]
        for r in examples:
            tt = " | ".join(t or "?" for t in r["marc_titles"])
            au = " / ".join(r["marc_authors"]) if r["marc_authors"] else "?"
            yr = " / ".join(str(y) for y in r["marc_years"])
            lines.append(
                f"- bibs: `{', '.join(r['bib_ids'])}`  \n"
                f"  authors: {au}  \n"
                f"  titles: {tt}  \n"
                f"  years: {yr}  \n"
                f"  rationale: {r['rationale']}\n"
            )
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nVerdicts → {verdicts_path}")
    print(f"Summary  → {summary_path}\n")
    print("Verdict distribution:")
    for verdict, count in verdict_counter.most_common():
        print(f"  {verdict:<42s} {count:>4}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="merge-clusters CSV from the SPARQL query (work, titles, bibIds, ...).",
    )
    parser.add_argument(
        "--marcxml-dir",
        type=Path,
        default=Path("marcxml/sierra"),
        help="Source MARCXML directory (default: marcxml/sierra/).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("scratchpad/merge-cluster-verdicts"),
        help="Output directory (default: scratchpad/merge-cluster-verdicts/).",
    )
    args = parser.parse_args(argv)
    return _process(args)


if __name__ == "__main__":
    sys.exit(main())

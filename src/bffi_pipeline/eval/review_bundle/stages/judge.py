"""Cataloguer review pipeline — M6 judge stage of the review bundle.

The M6 judge already writes ``runs/<uuid>/judge-decisions.jsonl`` per
run (via :func:`bffi_pipeline.stages.m6.sidecars._serialise_decision`)
— this handler consumes it as the candidate pool. Unlike contrib,
there's no corpus-wide aggregator: judge bundles are per-run only.

Each judge-decisions row carries the LLM's same_work / different_work
/ uncertain verdict on a pair of bffi:Works. To present that to the
cataloguer in a useful form, the sampler:

1. Translates ``work_a`` / ``work_b`` URIs to ``helmet_bib_id``s via
   the run's ``helmet-map.jsonl`` (composed with
   :func:`bffi_pipeline.uris.mint_raw_work_uri` so the M3-minted URI
   matches the judge sidecar's URI shape).
2. Hydrates each side's GoldRecord context (creator / title /
   language / year / content_type) from the run's per-record BFFI
   Turtle so the bundle's candidate rows are self-contained and the
   importer doesn't need to re-walk graphs.
3. Stratifies on ``decision x confidence band`` so the cataloguer
   sees a balanced mix of same/different/uncertain x low/mid/high
   confidence — the most useful distribution for spotting LLM
   over/under-confidence in particular.

The cataloguer's KEEP rows become :class:`~gold_set.GoldCase` rows
appended to ``gold/gold.jsonl`` (the existing 15-row bootstrap).
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.uris import mint_raw_work_uri

#: M6 already writes this per-run sidecar. The cataloguer-bundle stage
#: looks for it under ``<run_dir>/`` by name to decide whether to
#: include the judge tab in this run's bundle.
AUDIT_FILENAME: Final[str] = "judge-decisions.jsonl"

#: ``added_by`` value stamped on every KEEP row that lands in
#: ``gold/gold.jsonl``. Lets the eval harness tell M6-mined rows
#: apart from bootstrap + earlier-grown candidates.
ADDED_BY_PREFIX: Final[str] = "cataloguer-m6"

#: Categories the cataloguer can pick from. Mirrors
#: :data:`bffi_pipeline.eval.gold_set.GoldCategory`.
GOLD_CATEGORIES: Final[tuple[str, ...]] = (
    "translation",
    "transliteration",
    "adaptation",
    "abridgement",
    "common-title-collision",
    "compilation-vs-constituent",
    "edition-revision",
    "music-recording-vs-notated",
    "same-author-different-titles",
    "cross-genre-different-work",
    "subject-as-name-discrimination",
)

#: Decision values the HTML tool emits for judge pairs. ``AGREE`` keeps
#: the LLM's ``decision`` verbatim; ``FLIP-TO-*`` overrides it;
#: ``UNCERTAIN`` flags the pair for follow-up (drops it from gold);
#: ``DISCARD`` / ``SKIP-FOR-NOW`` mirror contrib semantics.
DECISION_AGREE: Final[str] = "AGREE"
DECISION_FLIP_SAME: Final[str] = "FLIP-TO-SAME"
DECISION_FLIP_DIFFERENT: Final[str] = "FLIP-TO-DIFFERENT"
DECISION_UNCERTAIN: Final[str] = "STILL-UNCERTAIN"
DECISION_DISCARD: Final[str] = "DISCARD"
DECISION_SKIP: Final[str] = "SKIP-FOR-NOW"

#: Decisions that land in gold/gold.jsonl. ``UNCERTAIN`` doesn't —
#: gold cases must have a binary expected value (same / different),
#: so still-uncertain pairs stay in the audit log for a future review.
_GOLD_LANDING_DECISIONS: Final[frozenset[str]] = frozenset(
    {DECISION_AGREE, DECISION_FLIP_SAME, DECISION_FLIP_DIFFERENT}
)

_VALID_DECISIONS: Final[frozenset[str]] = frozenset(
    {
        DECISION_AGREE,
        DECISION_FLIP_SAME,
        DECISION_FLIP_DIFFERENT,
        DECISION_UNCERTAIN,
        DECISION_DISCARD,
        DECISION_SKIP,
    }
)

#: Confidence bands used by the stratified sampler. Low-confidence
#: rows are the most useful for cataloguer review — they're where the
#: LLM is uncertain and a human eye matters most. The bands are
#: closed-open intervals on [0, 1].
_CONFIDENCE_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    ("low", 0.0, 0.6),
    ("mid", 0.6, 0.85),
    ("high", 0.85, 1.0001),
)

#: Cap on SKIP / composite-name ids printed in the summary preview.
_PREVIEW_CAP: Final[int] = 5


class JudgeImportError(ValueError):
    """Raised when a cataloguer result row fails schema validation.

    The error message names the offending row by ``id`` + the failing
    field so the operator can ask the cataloguer to fix one specific
    row rather than re-doing the whole batch.
    """


@dataclass
class ImportSummary:
    """Operator-facing summary printed by the CLI after a judge import."""

    rows_seen: int = 0
    landed: int = 0
    discarded: int = 0
    skipped: int = 0
    still_uncertain: int = 0
    gold_total_before: int = 0
    gold_total_after: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_expected: dict[str, int] = field(default_factory=dict)
    skipped_ids: list[str] = field(default_factory=list)
    flipped_ids: list[tuple[str, str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "judge-review-import summary",
            f"  rows in cataloguer file:        {self.rows_seen}",
            f"  Landed in gold/gold.jsonl:      {self.landed}",
            f"  DISCARD (dropped):              {self.discarded}",
            f"  SKIP-FOR-NOW (still pending):   {self.skipped}",
            f"  STILL-UNCERTAIN (no gold row):  {self.still_uncertain}",
        ]
        if self.by_category:
            lines.append("  Landed by category:")
            for cat, n in sorted(self.by_category.items()):
                lines.append(f"    {cat:35} {n}")
        if self.by_expected:
            lines.append("  Landed by expected:")
            for exp, n in sorted(self.by_expected.items()):
                lines.append(f"    {exp:35} {n}")
        lines.append(f"  gold/gold.jsonl: {self.gold_total_before} → {self.gold_total_after}")
        if self.skipped_ids:
            preview = ", ".join(self.skipped_ids[:_PREVIEW_CAP])
            extra = len(self.skipped_ids) - _PREVIEW_CAP
            more = "" if extra <= 0 else f" (and {extra} more)"
            lines.append(f"  SKIP-FOR-NOW ids: {preview}{more}")
        if self.flipped_ids:
            lines.append(f"  Cataloguer overrode the LLM on {len(self.flipped_ids)} pair(s):")
            for new_id, llm, expected in self.flipped_ids[:_PREVIEW_CAP]:
                lines.append(f"    {new_id}: llm={llm} → cataloguer={expected}")
            extra = len(self.flipped_ids) - _PREVIEW_CAP
            if extra > 0:
                lines.append(f"    (and {extra} more)")
        return "\n".join(lines)


# --- Run-context resolvers ----------------------------------------------


def _load_helmet_bib_index(run_dir: Path) -> dict[str, str]:
    """Build a ``{minted_work_uri → helmet_bib_id}`` lookup.

    M2 writes ``helmet-map.jsonl`` per run, recording each source bib's
    raw bibframe Work URI. M3's :func:`mint_raw_work_uri` hashes that
    URI to produce the canonical bffi:Work URI the judge sidecar
    references. We compose the two so this handler can resolve any
    work_a / work_b URI to a bib id without walking the graph.
    """
    helmet_map = run_dir / "helmet-map.jsonl"
    if not helmet_map.is_file():
        raise FileNotFoundError(
            f"helmet-map.jsonl not found at {helmet_map}. The judge handler "
            "needs it to translate work URIs to helmet_bib_ids. Did M2 run?"
        )
    index: dict[str, str] = {}
    with helmet_map.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            raw_work_uri = row.get("raw_work_uri")
            bib_id = row.get("helmet_bib_id")
            if not raw_work_uri or not bib_id:
                continue
            index[mint_raw_work_uri(raw_work_uri)] = bib_id
    return index


def _first_work_uri(g: Graph) -> URIRef | None:
    for s in g.subjects(RDF.type, V.BFFI.Work):
        if isinstance(s, URIRef):
            return s
    return None


def _first_title(g: Graph, work_uri: URIRef) -> str | None:
    for label in g.objects(work_uri, V.SKOS.prefLabel):
        if isinstance(label, Literal):
            return str(label)
    return None


def _first_creator(g: Graph, work_uri: URIRef) -> str | None:
    for contrib in g.objects(work_uri, V.BFFI.contribution):
        if (contrib, RDF.type, V.BFFI.PrimaryContribution) not in g:
            continue
        for agent in g.objects(contrib, V.BFFI.agent):
            for agent_label in g.objects(agent, RDFS.label):
                if isinstance(agent_label, Literal):
                    return str(agent_label)
    return None


def _expression_fields(g: Graph, work_uri: URIRef) -> dict[str, str]:
    out: dict[str, str] = {}
    for expr in g.objects(work_uri, V.BFFI.hasExpression):
        for lang in g.objects(expr, V.BFFI.language):
            if isinstance(lang, URIRef):
                # http://id.loc.gov/vocabulary/languages/fin → "fin"
                out["language"] = str(lang).rsplit("/", 1)[-1]
        for content in g.objects(expr, V.BFFI.content):
            if isinstance(content, URIRef):
                out["content_type"] = str(content).rsplit("/", 1)[-1]
        break
    return out


def _extract_gold_record(bffi_dir: Path, bib_id: str) -> dict[str, Any]:
    """Read ``<bffi_dir>/<bib_id>.ttl`` and pull GoldRecord context.

    Extracts ``creator`` (the primary Contribution's agent label),
    ``title`` (the Work's skos:prefLabel), ``language`` (the
    Expression's ``bffi:language`` URI, mapped back to its 3-letter
    code), ``content_type`` (the LoC vocabulary code).

    Missing fields stay absent rather than null'd — :class:`GoldRecord`
    treats them as ``None`` and the eval harness handles partial
    records gracefully. If the BFFI file itself is missing (M3
    dropped this record), returns a stub with just the bib id.
    """
    out: dict[str, Any] = {"helmet_bib_id": bib_id}
    ttl_path = bffi_dir / f"{bib_id}.ttl"
    if not ttl_path.is_file():
        return out

    g = Graph()
    try:
        g.parse(str(ttl_path), format="turtle")
    except Exception:
        return out

    work_uri = _first_work_uri(g)
    if work_uri is None:
        return out

    title = _first_title(g, work_uri)
    if title is not None:
        out["title"] = title
    creator = _first_creator(g, work_uri)
    if creator is not None:
        out["creator"] = creator
    out.update(_expression_fields(g, work_uri))
    return out


# --- Category auto-suggestion -------------------------------------------


def _suggest_category(decision: str, diverging_fields: list[str]) -> str:
    """Heuristic category suggestion for the cataloguer to override.

    The judge LLM exposes ``decision`` (same_work / different_work /
    uncertain) and a list of ``diverging_fields``. We map common
    diverging patterns to the GoldCategory enum; the cataloguer
    always has the last word.
    """
    diverging = set(diverging_fields)
    if decision == "same_work":
        # Same-work pairs are almost always translations or
        # adaptations or edition revisions. Picking "translation" as
        # the default is the most common case; cataloguer overrides
        # for the others.
        if "expression_language" in diverging:
            return "translation"
        return "edition-revision"
    # different_work / uncertain → most common defaults.
    if "preferred_title" in diverging and "creator" not in diverging:
        return "same-author-different-titles"
    return "cross-genre-different-work"


# --- Confidence bands ---------------------------------------------------


def _band_for(confidence: float) -> str:
    for label, lo, hi in _CONFIDENCE_BANDS:
        if lo <= confidence < hi:
            return label
    return "mid"


def _seed_to_int(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


# --- Sampler ------------------------------------------------------------


def sample_stratified(
    *,
    input_path: Path,
    output_path: Path,
    per_category: int = 25,
    seed: str | None = None,
) -> dict[str, int]:
    """Draw a stratified slice of the run's judge decisions.

    Strata are ``decision x confidence_band`` (3 decisions x 3 bands
    = 9 strata; in practice "uncertain" rows are rare). For each
    stratum, sample up to ``per_category`` rows seeded by ``seed``
    (default: today's UTC date). Hydrate each row's record_a /
    record_b GoldRecord fields from the run's BFFI graphs so the
    bundle is self-contained.

    Returns a per-stratum histogram of the sampled batch.
    """
    if seed is None:
        seed = datetime.now(UTC).date().isoformat()
    rng = random.Random(_seed_to_int(seed))

    run_dir = input_path.parent
    bib_index = _load_helmet_bib_index(run_dir)
    bffi_dir = run_dir / "bffi"

    # Bucket rows by (decision, confidence_band).
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    rows_seen = 0
    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            rows_seen += 1
            decision = row.get("decision", "uncertain")
            band = _band_for(float(row.get("confidence", 0.0)))
            buckets.setdefault((decision, band), []).append(row)

    histogram: dict[str, int] = {}
    picked: list[dict[str, Any]] = []
    for (decision, band), rows in sorted(buckets.items()):
        # ``per_category <= 0`` means "no cap" — see picker.py for the
        # full-audit motivation.
        chosen = rows if per_category <= 0 else rng.sample(rows, min(per_category, len(rows)))
        histogram[f"{decision}/{band}"] = len(chosen)
        picked.extend(chosen)

    # Build candidate rows. Hydrate GoldRecord fields per side.
    sequence = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()
    with output_path.open("w", encoding="utf-8") as out_fh:
        for row in picked:
            sequence += 1
            work_a = row["work_a"]
            work_b = row["work_b"]
            bib_a = bib_index.get(work_a)
            bib_b = bib_index.get(work_b)
            record_a = _extract_gold_record(bffi_dir, bib_a) if bib_a else {"work_uri": work_a}
            record_b = _extract_gold_record(bffi_dir, bib_b) if bib_b else {"work_uri": work_b}
            candidate = {
                "id": f"cg-pending-{sequence:04d}",
                "category": _suggest_category(
                    row.get("decision", "uncertain"),
                    list(row.get("diverging_fields", []) or []),
                ),
                "record_a": record_a,
                "record_b": record_b,
                "embedding_sim": row.get("similarity"),
                "llm_decision": row.get("decision"),
                "llm_confidence": row.get("confidence"),
                "llm_rationale": row.get("rationale"),
                "matching_fields": list(row.get("matching_fields", []) or []),
                "diverging_fields": list(row.get("diverging_fields", []) or []),
                "used_cascade": bool(row.get("used_cascade", False)),
                "holdout": False,
                "added": today,
                "added_by": "m6-cascade",
                "notes": "",
            }
            out_fh.write(json.dumps(candidate, ensure_ascii=False))
            out_fh.write("\n")

    return histogram


def bib_ids_for_marc(candidates_jsonl: bytes) -> list[str]:
    """Collect every helmet_bib_id on either side of every pair.

    The bundle builder dedupes downstream — many judge pairs share a
    Work, so the same bib_id appears on multiple rows. Returning all
    of them (with duplicates) is fine; the bundle's MARC sidecar
    attachment loop already dedups via ``seen_bibs``.
    """
    out: list[str] = []
    for raw in candidates_jsonl.decode("utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        for side in ("record_a", "record_b"):
            bib = row.get(side, {}).get("helmet_bib_id")
            if bib:
                out.append(bib)
    return out


# --- Importer -----------------------------------------------------------


def _validate_keep_row(row: dict[str, Any]) -> None:
    row_id = row.get("id", "<no-id>")
    expected = row.get("expected")
    if expected not in ("same_work", "different_work"):
        raise JudgeImportError(
            f"Row {row_id!r}: expected must be 'same_work' or 'different_work', "
            f"got {expected!r}. AGREE/FLIP-TO-* decisions resolve this; STILL-UNCERTAIN "
            "must not land in gold."
        )
    category = row.get("category")
    if category not in GOLD_CATEGORIES:
        raise JudgeImportError(
            f"Row {row_id!r}: category must be one of {GOLD_CATEGORIES}, got {category!r}."
        )
    for side in ("record_a", "record_b"):
        if not isinstance(row.get(side), dict):
            raise JudgeImportError(f"Row {row_id!r}: {side} must be an object.")


def _next_gold_id(gold_path: Path) -> int:
    """Return the next sequential ``gs-NNNN`` index by scanning the file."""
    if not gold_path.is_file():
        return 1
    max_seq = 0
    with gold_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            row_id = row.get("id", "")
            if not isinstance(row_id, str) or not row_id.startswith("gs-"):
                continue
            try:
                seq = int(row_id.removeprefix("gs-"))
            except ValueError:
                continue
            max_seq = max(max_seq, seq)
    return max_seq + 1


def _result_row_to_gold_row(row: dict[str, Any], *, new_id: str) -> dict[str, Any]:
    """Coerce a cataloguer's KEEP row into the GoldCase shape that
    ``gold/gold.jsonl`` carries. Strips cataloguer-only fields
    (``decision``, ``llm_*``, ``matching_fields``, ``diverging_fields``,
    ``reviewed_by``, ``reviewed_at``) and stamps ``added_by`` with the
    reviewer credit."""
    reviewer = row.get("reviewed_by") or ""
    added_by = f"{ADDED_BY_PREFIX}:{reviewer}" if reviewer else ADDED_BY_PREFIX
    today = datetime.now(UTC).date().isoformat()
    gold: dict[str, Any] = {
        "id": new_id,
        "category": row["category"],
        "expected": row["expected"],
        "holdout": bool(row.get("holdout", False)),
        "added": row.get("added") or today,
        "added_by": added_by,
        "notes": row.get("notes") or "",
        "record_a": row["record_a"],
        "record_b": row["record_b"],
    }
    sim = row.get("embedding_sim")
    if sim is not None:
        gold["embedding_sim"] = sim
    return gold


def import_results(*, input_path: Path, output_path: Path) -> ImportSummary:
    """Import a cataloguer's exported judge results.jsonl.

    Two-pass atomic: all KEEP rows validated before any landed.
    DISCARD / SKIP-FOR-NOW / STILL-UNCERTAIN don't land in gold but
    are counted in the summary.
    """
    rows: list[dict[str, Any]] = []
    with input_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))

    summary = ImportSummary(rows_seen=len(rows))
    if output_path.is_file():
        summary.gold_total_before = sum(
            1 for _ in output_path.read_text(encoding="utf-8").splitlines() if _.strip()
        )

    next_seq = _next_gold_id(output_path)

    landing_rows: list[dict[str, Any]] = []
    for row in rows:
        decision = row.get("decision")
        if decision not in _VALID_DECISIONS:
            row_id = row.get("id", "<no-id>")
            raise JudgeImportError(
                f"Row {row_id!r}: decision must be one of {sorted(_VALID_DECISIONS)}, "
                f"got {decision!r}."
            )
        if decision in _GOLD_LANDING_DECISIONS:
            _validate_keep_row(row)
            landing_rows.append(row)
        elif decision == DECISION_DISCARD:
            summary.discarded += 1
        elif decision == DECISION_UNCERTAIN:
            summary.still_uncertain += 1
        else:  # DECISION_SKIP
            summary.skipped += 1
            summary.skipped_ids.append(str(row.get("id", "<no-id>")))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as fh:
        for row in landing_rows:
            new_id = f"gs-{next_seq:04d}"
            next_seq += 1
            gold_row = _result_row_to_gold_row(row, new_id=new_id)
            fh.write(json.dumps(gold_row, ensure_ascii=False))
            fh.write("\n")
            summary.landed += 1
            cat = gold_row["category"]
            summary.by_category[cat] = summary.by_category.get(cat, 0) + 1
            expected = gold_row["expected"]
            summary.by_expected[expected] = summary.by_expected.get(expected, 0) + 1
            llm = row.get("llm_decision")
            if llm and llm not in (expected, "uncertain"):
                summary.flipped_ids.append((new_id, llm, expected))

    summary.gold_total_after = summary.gold_total_before + summary.landed
    return summary


__all__ = [
    "ADDED_BY_PREFIX",
    "AUDIT_FILENAME",
    "DECISION_AGREE",
    "DECISION_DISCARD",
    "DECISION_FLIP_DIFFERENT",
    "DECISION_FLIP_SAME",
    "DECISION_SKIP",
    "DECISION_UNCERTAIN",
    "GOLD_CATEGORIES",
    "ImportSummary",
    "JudgeImportError",
    "bib_ids_for_marc",
    "import_results",
    "sample_stratified",
]

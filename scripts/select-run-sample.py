"""Select a stratified MARCXML sample for a pipeline run.

Strategy
--------
The 800 k Helmet MARCXML corpus is too big for a single cold-cache
full-pipeline run on the M2 Max (≈ 5 h per 5 k records cold, dominated
by M6 LLM judging). Operator-driven sub-corpus runs (smoke tests,
overnight benchmarks, cataloguer-audit slices) are the practical
unit. This script selects a deterministic stratified subset that:

1. Validates **P-15** M3 authority-URI preservation on a corpus
   slice big enough to surface edge cases (the 19-record 2026-05-13
   audit only exercised one ``651`` case).
2. Probes **P-16** fallback-tier knobs on a wider sample where
   namesake-rich common names (Williams / Karjalainen / Smith) will
   show up naturally.
3. Produces a P-14-shaped audit JSONL that the cataloguer engagement
   can act on without re-running the pipeline.
4. Measures cold + warm wall-time on a representative slice for the
   P-10 plan's full-corpus extrapolation table.

Sample composition (fractions; absolute counts scale with ``--n``):

| Stratum | Fraction | Rationale |
|---|---:|---|
| Random baseline | **70%** | Broad coverage of the corpus; statistically representative for wall-time + outcome-rate measurement. |
| ``has_651`` (geographic subjects) | **10%** | Direct P-15 validation — these records carry the `bf:Place` + `madsrdf:isIdentifiedByAuthority` shape the M3 fix targets. |
| ``has_bilingual_yso`` (yso/fin + yso/swe in same record) | **5%** | The b26322791 pattern at scale. Confirms the M3 fix collapses bilingual concepts to one URI consistently. |
| ``has_bilingual_slm`` (slm/fin + slm/swe) | **2.5%** | SLM (genre/form) bilingual companion; same shape as yso but a smaller vocab. |
| ``has_fictional`` (cataloguer-marked fictional characters) | **5%** | The fictional-character marker outcome that short-circuits reconcile; we want to confirm no regression. |
| ``has_kanto`` (records carrying FI-ASTERI-N $0 on 1XX/7XX) | **7.5%** | Common authors going through tier-2 LLM picker today (per the 2026-05-13 audit's Category 1 finding); these are the prime candidates for short-circuiting via tier-0 once P-14 Phase C lands. |
| Cataloguer-curated 19 + similar shape | (always included) | Guaranteed inclusion of the 2026-05-13 email's bib IDs so the audit can diff against the prior verdict. |

Strata may overlap (a record can be both ``has_651`` AND ``has_bilingual_yso``). The deduplication step keeps each record once and records all matching stratum tags on its row. Strata are sampled top-down in the order above so over-quota strata don't crowd out under-quota ones.

Sampling is deterministic via ``--seed`` (default 42) so the sample is reproducible.

Output
------
``<out-dir>/`` (default: ``marcxml/samples/helmet/<n>/``)
  ``sample.jsonl`` — one row per record:
    ``{"bib_id": "b...", "strata": ["random", "has_651", ...]}``
  ``marcxml/`` — symlinks to the selected MARCXML files (atomically replaceable; downstream stages take this as their MARCXML_DIR).
  ``manifest.json`` — sampling metadata (seed, scan timestamp, stratum-bucket sizes pre-sample).

Feature index (cached scan result)
----------------------------------
The corpus scan is the slowest step (~5 min on the M2 Max for 800 k
files). To avoid re-paying it on every re-sample, the scan result is
persisted as a JSON index at ``<marcxml-dir>/../.feature-index.json``
by default (``marcxml/.feature-index.json`` for the canonical layout —
sits inside the gitignored ``marcxml/`` tree so it ships with the
corpus). The index encodes ``all_bib_ids`` plus each feature bucket
keyed by name, plus a ``schema_version`` and ``file_count`` for
staleness detection.

On invocation:

- Index missing → scan + write index + sample.
- Index present, ``schema_version`` + ``file_count`` match → load
  index, skip scan, sample.
- Index present, schema or count mismatch → rebuild + sample.
- ``--rebuild-index`` → force rebuild (use after the corpus mutates
  in place without changing file count).
- ``--no-index`` → scan every invocation; never touch the index file.

Subsequent runs (different ``--n``, different ``--seed``, different
``--out-dir``) thus complete in seconds rather than minutes.

Usage
-----
::

    uv run python scripts/select-run-sample.py \\
        --marcxml-dir marcxml/sierra \\
        --out-dir marcxml/samples/helmet/5000 \\
        --n 5000 \\
        [--seed 42] \\
        [--index-path marcxml/.feature-index.json] \\
        [--rebuild-index | --no-index]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Final

#: Default stratum quotas (counts at n=20_000). Re-scale linearly if
#: ``--n`` differs from 20_000.
_DEFAULT_N: Final[int] = 20_000
_STRATUM_FRACTIONS: Final[dict[str, float]] = {
    # The ``random`` stratum is everyone in the corpus; named here for
    # quota allocation only.
    "random":             0.70,  # 14 000
    "has_651":            0.10,  # 2 000  — P-15 validation
    "has_bilingual_yso":  0.05,  # 1 000  — b26322791-shaped
    "has_bilingual_slm":  0.025,  # 500   — same shape, SLM vocab
    "has_fictional":      0.05,  # 1 000  — fictional-character outcome
    "has_kanto":          0.075,  # 1 500  — common-author tier-2 LLM path
}

#: Cataloguer-curated bib IDs from the 2026-05-13 email; always included
#: regardless of stratum quotas, so the next audit can directly diff
#: against the prior verdict.
_CATALOGUER_PINS: Final[tuple[str, ...]] = (
    "b26152228", "b26141280", "b13164132", "b15641065", "b26267822",
    "b22522396", "b23948619", "b26485916", "b25845469", "b23481833",
    "b25254509", "b23591146", "b26163743", "b22057407",
    "b26356557", "b26304119", "b26322791", "b2635665x", "b26346564",
)

#: Default filename for the cached feature index. Lives one level above
#: the marcxml dir by default (e.g. ``marcxml/.feature-index.json`` for
#: a ``marcxml/sierra`` corpus), which keeps it gitignored alongside the
#: corpus it describes and lets repeat sampling skip the ~5 min scan.
_INDEX_FILENAME: Final[str] = ".feature-index.json"

#: Schema version of the on-disk index. Bump when the feature-bucket
#: layout or any of the substring patterns in :func:`scan_corpus`
#: changes; a mismatch forces a rebuild.
_INDEX_SCHEMA_VERSION: Final[int] = 1


def scan_corpus(marcxml_dir: Path) -> tuple[list[str], dict[str, set[str]]]:
    """Single-pass scan: classify every MARCXML file into feature buckets.

    Returns ``(all_bib_ids, bucket_to_bib_ids)``. The scan reads each
    file fully and substring-matches against feature markers. Buckets
    are *sets* so over-quota records can be deduplicated by the caller
    without ordering surprises.
    """
    all_bib_ids: list[str] = []
    buckets: dict[str, set[str]] = {key: set() for key in _STRATUM_FRACTIONS if key != "random"}

    files = sorted(marcxml_dir.glob("*.xml"))
    total = len(files)
    print(f"[scan] {total:,} MARCXML files under {marcxml_dir}", file=sys.stderr)
    progress_every = max(1, total // 20)

    for i, path in enumerate(files, start=1):
        bib_id = path.stem
        all_bib_ids.append(bib_id)
        # Single read per file; cheap substring matching beats opening
        # the file multiple times for different regexes.
        text = path.read_text(encoding="utf-8", errors="replace")
        if 'tag="651"' in text:
            buckets["has_651"].add(bib_id)
        has_yso_swe = "yso/swe" in text
        has_yso_fin = "yso/fin" in text
        if has_yso_swe and has_yso_fin:
            buckets["has_bilingual_yso"].add(bib_id)
        has_slm_swe = "slm/swe" in text
        has_slm_fin = "slm/fin" in text
        if has_slm_swe and has_slm_fin:
            buckets["has_bilingual_slm"].add(bib_id)
        if "fiktiivinen" in text:
            buckets["has_fictional"].add(bib_id)
        if "FI-ASTERI-N" in text:
            buckets["has_kanto"].add(bib_id)
        if i % progress_every == 0:
            print(f"[scan]   {i:>7,}/{total:,} ({100 * i // total:>3}%)", file=sys.stderr)

    print(f"[scan] done. bucket sizes:", file=sys.stderr)
    for key, members in sorted(buckets.items()):
        print(f"[scan]   {key:<22s} {len(members):>7,}", file=sys.stderr)
    return all_bib_ids, buckets


# --- Feature index (cache for repeat sampling) -----------------------------


def load_index(path: Path) -> dict | None:
    """Read a previously-written feature index from disk.

    Returns the parsed payload, or ``None`` if the file is missing or
    unreadable (caller falls back to a fresh corpus scan).
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[index] {path} unreadable ({exc}); will rebuild", file=sys.stderr)
        return None


def is_index_stale(index: dict, marcxml_dir: Path) -> tuple[bool, str]:
    """Decide whether ``index`` still matches ``marcxml_dir`` on disk.

    Returns ``(is_stale, reason)``. ``reason`` is empty when the index
    is fresh. Cheap signals only: schema version + file count. A
    same-count-different-files mutation slips through — the operator
    forces a rebuild via ``--rebuild-index`` when they know the corpus
    actually changed.
    """
    if index.get("schema_version") != _INDEX_SCHEMA_VERSION:
        return (
            True,
            f"schema_version mismatch "
            f"(have {index.get('schema_version')}, want {_INDEX_SCHEMA_VERSION})",
        )
    current_count = sum(1 for _ in marcxml_dir.glob("*.xml"))
    if current_count != index.get("file_count"):
        return (
            True,
            f"file count changed ({index.get('file_count')} → {current_count})",
        )
    return False, ""


def save_index(
    path: Path,
    *,
    all_bib_ids: list[str],
    buckets: dict[str, set[str]],
    source_dir: Path,
) -> None:
    """Persist the scan result atomically as a single JSON document.

    The buckets are sorted on the way out so the file diffs cleanly
    across rebuilds (deterministic byte layout for ``random.shuffle``-
    seeded reproducibility checks). Atomic via ``.tmp`` + ``os.replace``
    so a crashed writer leaves any previous index intact.
    """
    payload = {
        "schema_version": _INDEX_SCHEMA_VERSION,
        "source_dir": str(source_dir.resolve()),
        "file_count": len(all_bib_ids),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "all_bib_ids": all_bib_ids,
        "buckets": {key: sorted(values) for key, values in buckets.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_or_build_index(
    *,
    marcxml_dir: Path,
    index_path: Path,
    rebuild: bool,
    use_index: bool,
) -> tuple[list[str], dict[str, set[str]]]:
    """Return ``(all_bib_ids, buckets)`` either from cache or by scanning.

    Decision tree:

    1. ``use_index=False`` → scan fresh, don't touch the index file.
    2. ``rebuild=True`` → scan fresh, overwrite the index.
    3. Existing index that's not stale → load + return; no scan.
    4. Missing or stale index → scan, write, return.
    """
    if not use_index:
        print(f"[index] --no-index: scanning {marcxml_dir} without caching", file=sys.stderr)
        return scan_corpus(marcxml_dir)

    if not rebuild:
        loaded = load_index(index_path)
        if loaded is not None:
            stale, reason = is_index_stale(loaded, marcxml_dir)
            if not stale:
                print(
                    f"[index] loaded {index_path} "
                    f"({loaded['file_count']:,} files, "
                    f"generated {loaded['generated_at']})",
                    file=sys.stderr,
                )
                all_bib_ids: list[str] = loaded["all_bib_ids"]
                buckets: dict[str, set[str]] = {
                    key: set(values) for key, values in loaded["buckets"].items()
                }
                # Forward-compat: surface bucket sizes the way scan_corpus does.
                print(f"[index] bucket sizes:", file=sys.stderr)
                for key, members in sorted(buckets.items()):
                    print(f"[index]   {key:<22s} {len(members):>7,}", file=sys.stderr)
                return all_bib_ids, buckets
            print(f"[index] {index_path} is stale ({reason}); rebuilding", file=sys.stderr)
        else:
            print(f"[index] no cached index at {index_path}; building", file=sys.stderr)
    else:
        print(f"[index] --rebuild-index: forcing rebuild at {index_path}", file=sys.stderr)

    all_bib_ids, buckets = scan_corpus(marcxml_dir)
    save_index(
        index_path,
        all_bib_ids=all_bib_ids,
        buckets=buckets,
        source_dir=marcxml_dir,
    )
    print(f"[index] wrote {len(all_bib_ids):,} records to {index_path}", file=sys.stderr)
    return all_bib_ids, buckets


def pick_sample(
    all_bib_ids: list[str],
    buckets: dict[str, set[str]],
    *,
    n: int,
    seed: int,
) -> dict[str, set[str]]:
    """Apply the stratum quotas top-down with deterministic sampling.

    Returns ``selected_bib_id -> set_of_stratum_tags``. A record that
    landed in multiple feature buckets carries all of its tags.

    The strategy is order-sensitive: smaller-bucket strata (e.g.
    ``has_bilingual_slm``) are filled FIRST so the wider strata
    (``random``, ``has_651``) don't accidentally exhaust the small
    pool through overlap.
    """
    rng = random.Random(seed)
    chosen: dict[str, set[str]] = {}

    # Compute quotas in ascending bucket-size order so small / rare
    # buckets get their allocation before the wide ones drain the pool.
    order = sorted(buckets, key=lambda k: len(buckets[k]))
    print(f"[sample] target n = {n}, seed = {seed}", file=sys.stderr)
    for key in order:
        quota = round(n * _STRATUM_FRACTIONS[key])
        available = sorted(buckets[key])  # sort for deterministic shuffling
        rng.shuffle(available)
        picked_here = available[:quota]
        for bib_id in picked_here:
            chosen.setdefault(bib_id, set()).add(key)
        print(
            f"[sample]   {key:<22s} quota={quota:>5,}  available={len(buckets[key]):>7,}  picked={len(picked_here):>5,}",
            file=sys.stderr,
        )

    # Pin the cataloguer-curated bib IDs (override quota).
    for bib_id in _CATALOGUER_PINS:
        if bib_id in all_bib_ids:
            chosen.setdefault(bib_id, set()).add("cataloguer_pin")

    # Pad up to n from the random baseline. The random stratum's
    # allocation is whatever's left after the strata + pins; on the
    # 800 k corpus that's typically ~14 k.
    remaining = n - len(chosen)
    if remaining > 0:
        pool = [b for b in all_bib_ids if b not in chosen]
        rng.shuffle(pool)
        for bib_id in pool[:remaining]:
            chosen.setdefault(bib_id, set()).add("random")
    elif remaining < 0:
        # Strata overfilled — trim by dropping records that have only
        # the smallest, most-common stratum tag.
        print(
            f"[sample] strata overfilled by {-remaining}; trimming least-tagged records",
            file=sys.stderr,
        )
        sortable = sorted(chosen.items(), key=lambda kv: (len(kv[1]), kv[0]))
        for bib_id, _tags in sortable[:-n]:
            del chosen[bib_id]

    return chosen


def write_outputs(
    *,
    out_dir: Path,
    marcxml_dir: Path,
    chosen: dict[str, set[str]],
    all_bib_ids_count: int,
    buckets: dict[str, set[str]],
    seed: int,
) -> None:
    """Materialise the sample as JSONL + symlinks + manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    symlink_dir = out_dir / "marcxml"
    symlink_dir.mkdir(exist_ok=True)
    # Remove any prior symlinks in the dir (idempotent re-runs).
    for old in symlink_dir.iterdir():
        if old.is_symlink():
            old.unlink()

    # Symlink each selected MARCXML file so the pipeline can take this
    # directory as MARCXML_DIR without copying ~100 MB of source.
    for bib_id in chosen:
        src = (marcxml_dir / f"{bib_id}.xml").resolve()
        if not src.exists():
            print(f"[warn] source missing for {bib_id}; skipping symlink", file=sys.stderr)
            continue
        (symlink_dir / f"{bib_id}.xml").symlink_to(src)

    # sample.jsonl — one row per record, stratum tags sorted for stability.
    sample_path = out_dir / "sample.jsonl"
    with sample_path.open("w", encoding="utf-8") as f:
        for bib_id in sorted(chosen):
            f.write(
                json.dumps(
                    {"bib_id": bib_id, "strata": sorted(chosen[bib_id])},
                    ensure_ascii=False,
                )
                + "\n"
            )

    # manifest.json — sampling metadata.
    manifest = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "seed": seed,
        "n_selected": len(chosen),
        "corpus_size": all_bib_ids_count,
        "stratum_fractions": _STRATUM_FRACTIONS,
        "stratum_bucket_sizes": {k: len(v) for k, v in buckets.items()},
        "cataloguer_pins_count": len(_CATALOGUER_PINS),
        "marcxml_source_dir": str(marcxml_dir.resolve()),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Final distribution summary.
    tag_counter: Counter[str] = Counter()
    for tags in chosen.values():
        for tag in tags:
            tag_counter[tag] += 1
    print(
        f"\n[done] {len(chosen):,} bib_ids selected → {out_dir}",
        file=sys.stderr,
    )
    print(f"[done] strata coverage (records may carry multiple tags):", file=sys.stderr)
    for tag, count in tag_counter.most_common():
        print(f"[done]   {tag:<22s} {count:>6,}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--marcxml-dir",
        type=Path,
        default=Path("marcxml/sierra"),
        help="Directory containing per-bib MARCXML files (default: marcxml/sierra/).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=(
            "Output directory (default: marcxml/samples/helmet/<n>/). "
            "Will be created if missing; existing contents are preserved except "
            "symlinks under <out-dir>/marcxml/ which are replaced."
        ),
    )
    parser.add_argument("--n", type=int, default=_DEFAULT_N, help=f"Target sample size (default: {_DEFAULT_N})")
    parser.add_argument("--seed", type=int, default=42, help="random.Random seed (default: 42)")
    parser.add_argument(
        "--index-path",
        type=Path,
        default=None,
        help=(
            "Path to the cached feature index JSON. Default: "
            "<marcxml-dir>/../.feature-index.json. On first run the "
            "script scans the corpus and writes the index; subsequent "
            "runs read the index instead, skipping the ~5 min full-corpus "
            "scan."
        ),
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help=(
            "Force a fresh corpus scan and overwrite the cached index. "
            "Use after the corpus mutates in place (e.g. a partial re-export "
            "that keeps the file count stable)."
        ),
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help=(
            "Scan the corpus fresh every invocation; do not read or write "
            "the index file. Useful for one-shot sampling against a "
            "throwaway corpus mount."
        ),
    )
    args = parser.parse_args(argv)

    if not args.marcxml_dir.is_dir():
        print(f"ERROR: --marcxml-dir not a directory: {args.marcxml_dir}", file=sys.stderr)
        return 2

    out_dir = args.out_dir
    if out_dir is None:
        out_dir = Path("marcxml") / "samples" / "helmet" / str(args.n)

    index_path = args.index_path
    if index_path is None:
        index_path = args.marcxml_dir.parent / _INDEX_FILENAME

    all_bib_ids, buckets = load_or_build_index(
        marcxml_dir=args.marcxml_dir,
        index_path=index_path,
        rebuild=args.rebuild_index,
        use_index=not args.no_index,
    )
    chosen = pick_sample(all_bib_ids, buckets, n=args.n, seed=args.seed)
    write_outputs(
        out_dir=out_dir,
        marcxml_dir=args.marcxml_dir,
        chosen=chosen,
        all_bib_ids_count=len(all_bib_ids),
        buckets=buckets,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

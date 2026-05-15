"""M8 operator entry point — :func:`apply_merge`.

End-to-end orchestration: load M6's judge-decisions JSONL, run
union-find over ``same_work`` edges, mint canonical Works for each
group whose anchor carries the necessary inputs, emit five sidecars
(canonical.ttl + canonical-map.jsonl + canonical-conflicts.jsonl +
canonical-mint-failures.jsonl + .tsv), bind contributor variants
from the M3 cascade, and route mint-failures + conflicts into the
unified cataloguer-review TSVs.

P-38 Phase D: extracted from m8/runner.py. No logic change.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from rdflib import Graph, URIRef

from bffi_pipeline.cataloguer_review import append_source_row, append_target_row
from bffi_pipeline.config import get_settings
from bffi_pipeline.contrib_variants import DEFAULT_SIDECAR_NAME as VARIANTS_SIDECAR_NAME
from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.stages.m8.contribution_variants import _apply_contrib_variants
from bffi_pipeline.stages.m8.corpus import _load_work_records_from_corpus
from bffi_pipeline.stages.m8.decisions import _detect_conflicts, _load_decisions
from bffi_pipeline.stages.m8.helmet_map import _load_helmet_map
from bffi_pipeline.stages.m8.mint import (
    _bind_prefixes,
    _emit_canonical_work,
    _select_description_modifier,
)
from bffi_pipeline.stages.m8.schemas import (
    _M8_PROGRESS_CADENCE,
    CANONICAL_CONFLICTS_FILENAME,
    CANONICAL_FILENAME,
    CANONICAL_MAP_FILENAME,
    CANONICAL_MINT_FAILURES_FILENAME,
    CANONICAL_MINT_FAILURES_TSV_FILENAME,
    HELMET_MAP_FILENAME,
    JUDGE_DECISIONS_FILENAME,
    CanonicalEntry,
    CanonicalWorkInputs,
    HelmetMapEntry,
    JudgeDecisionRow,
    MergeResult,
    MintFailure,
)
from bffi_pipeline.stages.m8.sidecars import (
    _emit_canonical_map,
    _emit_conflicts,
    _emit_mint_failures,
    _emit_mint_failures_tsv,
)
from bffi_pipeline.stages.m8.union_find import _UnionFind
from bffi_pipeline.uris import mint_work_uri


def apply_merge(  # noqa: PLR0912, PLR0915 — terminal-step orchestrator: loads decisions, runs union-find, mints canonical, emits five sidecars + two cataloguer-review TSVs. Splitting fragments shared state (canonical_entries, conflicts, mint_failures) across helpers.
    decisions_path: Path | None = None,
    bffi_corpus_dir: Path | None = None,
    *,
    output_path: Path | None = None,
    map_path: Path | None = None,
    conflicts_path: Path | None = None,
    mint_failures_path: Path | None = None,
    mint_failures_tsv_path: Path | None = None,
    helmet_map_path: Path | None = None,
    variants_sidecar_path: Path | None = None,
    work_records: dict[str, CanonicalWorkInputs] | None = None,
    helmet_entries: dict[str, HelmetMapEntry] | None = None,
    now: datetime | None = None,
) -> MergeResult:
    """Apply judge decisions to mint canonical Works (M8)."""
    settings = get_settings()
    decisions_path = decisions_path or (settings.data_dir / JUDGE_DECISIONS_FILENAME)
    bffi_corpus_dir = bffi_corpus_dir or settings.data_dir
    output_path = output_path or (settings.data_dir / CANONICAL_FILENAME)
    map_path = map_path or (settings.data_dir / CANONICAL_MAP_FILENAME)
    conflicts_path = conflicts_path or (settings.data_dir / CANONICAL_CONFLICTS_FILENAME)
    # Default mint-failures alongside ``output_path`` (not via
    # ``settings.data_dir``) so tests passing only ``output_path`` find
    # an mkdir'd parent. ``output_path.parent`` always equals
    # ``settings.data_dir`` in the no-override case, so production
    # callers see the same layout as before.
    mint_failures_path = mint_failures_path or (
        output_path.parent / CANONICAL_MINT_FAILURES_FILENAME
    )
    mint_failures_tsv_path = mint_failures_tsv_path or (
        output_path.parent / CANONICAL_MINT_FAILURES_TSV_FILENAME
    )
    helmet_map_path = helmet_map_path or (settings.data_dir / HELMET_MAP_FILENAME)
    variants_sidecar_path = variants_sidecar_path or (settings.data_dir / VARIANTS_SIDECAR_NAME)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged_at = (now or datetime.now(UTC)).replace(microsecond=0)

    # P-18: emit ``start`` immediately so the dashboard shows M8 as
    # running during the BFFI-corpus load. On the 20 k bench the load
    # is ~8 min wall before any other M8 work; without the early
    # ``start`` the dashboard reports M8 as ``pending`` the whole
    # time. The canonical-group count isn't known yet — it lands in
    # the ``phase_boundary`` event below, once union-find completes.
    emit_if_active(stage="m8", event="start")

    decisions = _load_decisions(decisions_path)
    same_work_edges = [(d.work_a, d.work_b) for d in decisions if d.decision == "same_work"]
    different_work_edges = [
        (d.work_a, d.work_b) for d in decisions if d.decision == "different_work"
    ]
    uncertain_count = sum(1 for d in decisions if d.decision == "uncertain")

    if work_records is None:
        work_records = _load_work_records_from_corpus(bffi_corpus_dir)
    if helmet_entries is None:
        helmet_entries = _load_helmet_map(helmet_map_path)

    uf = _UnionFind()
    for w in work_records:
        uf.add(w)
    for a, b in same_work_edges:
        uf.add(a)
        uf.add(b)
        uf.union(a, b)

    groups = uf.groups()
    conflicts = _detect_conflicts(groups, different_work_edges, same_work_edges)
    conflict_roots = {sorted(c.members)[0] for c in conflicts}

    decisions_by_pair: dict[frozenset[str], JudgeDecisionRow] = {
        frozenset({d.work_a, d.work_b}): d for d in decisions
    }

    # P-18: ``start`` already emitted at the top of the function;
    # this event marks the load + union-find phase boundary and
    # carries the canonical-group count as the ETA denominator for
    # the emit loop below. Pattern mirrors M9's phase_boundary
    # events between Phase 1 / 1.5 / 2 / 3.
    emit_if_active(
        stage="m8",
        event="phase_boundary",
        phase="emit",
        counters={"total": len(groups)},
    )

    g = Graph()
    _bind_prefixes(g)
    canonical_entries: list[CanonicalEntry] = []

    sorted_roots = sorted(groups)
    mint_failures: list[MintFailure] = []
    for processed, root in enumerate(sorted_roots, start=1):
        member_uris = groups[root]
        if member_uris[0] in conflict_roots:
            continue  # Flagged for review; do not silently merge.
        members = [work_records[u] for u in member_uris if u in work_records]
        if not members:
            continue
        anchor = members[0]
        if not anchor.creator_uri or not anchor.pref_label:
            # Anchor lacks the canonical-key inputs. Most often this is
            # the anonymous-main-entry cataloguing pattern (no MARC 1XX
            # primary creator on edited compilations). Route to
            # ``canonical-mint-failures.jsonl`` — a SEPARATE file from
            # ``canonical-conflicts.jsonl`` so cataloguer review of real
            # M6 same/different contradictions isn't drowned in noise.
            # See the docstring on :class:`MintFailure`.
            missing: list[str] = []
            if not anchor.creator_uri:
                missing.append("creator_uri")
            if not anchor.pref_label:
                missing.append("pref_label")
            bib_ids = sorted({bib_id for m in members for _, bib_id in m.helmet_identifiers})
            mint_failures.append(
                MintFailure(
                    members=member_uris,
                    anchor_work_uri=anchor.work_uri,
                    missing_inputs=missing,
                    pref_label=anchor.pref_label,
                    helmet_bib_ids=bib_ids,
                )
            )
            continue
        canonical_uri = URIRef(mint_work_uri(anchor.creator_uri, anchor.pref_label))
        modifier = _select_description_modifier(members, decisions_by_pair)
        entry, _ = _emit_canonical_work(
            g,
            canonical_uri=canonical_uri,
            pref_label=anchor.pref_label,
            members=members,
            helmet_entries=helmet_entries,
            description_modifier_uri=modifier,
            description_change_date=merged_at,
            mint_anchor=anchor.mint_anchor,
        )
        canonical_entries.append(entry)
        if processed % _M8_PROGRESS_CADENCE == 0 or processed == len(sorted_roots):
            emit_if_active(
                stage="m8",
                event="progress",
                counters={"processed": processed, "total": len(sorted_roots)},
                extra={"canonical_works": len(canonical_entries)},
            )

    # F2: bind variant labels from the M3 cascade's sidecar onto the
    # canonical agents that match (canonical_label → existing rdfs:label
    # on the canonical Work / Expression's agents). Skipped silently
    # when the sidecar is absent (no cascade ran, or it ran without
    # detecting any variants).
    variants_bound = _apply_contrib_variants(
        g,
        variants_sidecar_path=variants_sidecar_path,
        canonical_entries=canonical_entries,
    )

    # Atomic-rename writes
    tmp_ttl = output_path.with_suffix(output_path.suffix + ".tmp")
    g.serialize(destination=str(tmp_ttl), format="turtle")
    tmp_ttl.replace(output_path)
    _emit_canonical_map(map_path, canonical_entries)
    _emit_conflicts(conflicts_path, conflicts)
    _emit_mint_failures(mint_failures_path, mint_failures)
    _emit_mint_failures_tsv(mint_failures_tsv_path, mint_failures)
    # P-31 Phase B: mint failures → unified source-review TSV (one row
    # per (mint_failure, helmet_bib_id) so the cataloguer's spreadsheet
    # row maps 1:1 to a Sierra record to inspect).
    for mf in mint_failures:
        details = f"missing inputs: {', '.join(mf.missing_inputs)}"
        for bib_id in mf.helmet_bib_ids or [mf.anchor_work_uri]:
            append_source_row(
                bib_id=bib_id,
                stage="m8",
                severity="blocking",
                details=details,
            )
    # P-31 Phase C: conflict groups → unified target-review TSV. These
    # are the M6 judge contradictions M8 refused to merge — highest-
    # yield bug-report candidates since the cascade already surfaced
    # uncertainty. ``member_bib_ids`` lets the cataloguer estimate
    # severity by inspecting the source MARC records directly (a
    # conflict spanning two well-known authors is more impactful than
    # one between two obscure ones).
    for c in conflicts:
        if not c.members:
            continue
        conflict_bib_ids: list[str] = []
        for raw_uri in c.members:
            helmet_entry = helmet_entries.get(raw_uri)
            if helmet_entry is not None:
                conflict_bib_ids.append(helmet_entry.helmet_bib_id)
        pair_a, pair_b = c.conflicting_pair
        path_str = " → ".join(f"{a}={b}" for a, b in c.same_work_path) or "(none)"
        details = (
            f"M6 judge said {pair_a} ≠ {pair_b}, but the same-work chain "
            f"({path_str}) merged them via union-find. "
            f"{len(c.members)} member(s) in the cycle. "
            f"Inspect the source MARC for each member to decide which edge was wrong."
        )
        append_target_row(
            member_bib_ids=conflict_bib_ids,
            reason="m8-conflict",
            confidence=None,
            details=details,
            dedup_key=c.members[0],
        )
    del variants_bound  # value is for future telemetry; not yet exposed

    emit_if_active(
        stage="m8",
        event="end",
        counters={
            "total_works": len(work_records),
            "canonical_works": len(canonical_entries),
            "conflict_groups": len(conflicts),
            "mint_failures": len(mint_failures),
            "same_work_decisions": len(same_work_edges),
            "different_work_decisions": len(different_work_edges),
            "uncertain_decisions": uncertain_count,
        },
    )
    return MergeResult(
        total_works=len(work_records),
        same_work_decisions=len(same_work_edges),
        different_work_decisions=len(different_work_edges),
        uncertain_decisions=uncertain_count,
        canonical_works=len(canonical_entries),
        conflict_groups=len(conflicts),
        mint_failures=len(mint_failures),
        canonical_path=str(output_path),
        map_path=str(map_path),
        conflicts_path=str(conflicts_path),
        mint_failures_path=str(mint_failures_path),
        mint_failures_tsv_path=str(mint_failures_tsv_path),
    )


def _iter_member_pairs(uf_groups: dict[str, list[str]]) -> Iterator[tuple[str, str]]:
    for members in uf_groups.values():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                yield (a, b)

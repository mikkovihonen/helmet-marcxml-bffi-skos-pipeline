"""Unit tests for stages/merge (M8 — canonical Work minting).

Tests against synthetic in-memory work_records and decisions JSONL —
no BFFI Turtle parsing here. The graph-extraction path is exercised
indirectly by the M2 → M3 → M8 integration test (future work); the
core merge / conflict / AdminMetadata logic is independent of
where the work_records dict came from and can be unit-tested
directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, URIRef

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.merge import (
    CanonicalWorkInputs,
    HelmetMapEntry,
    SubjectTarget,
    apply_merge,
)

# --- Test helpers ---------------------------------------------------------


WORK_A = "http://urn.fi/URN:NBN:fi:bib:work:aaa"
WORK_B = "http://urn.fi/URN:NBN:fi:bib:work:bbb"
WORK_C = "http://urn.fi/URN:NBN:fi:bib:work:ccc"

EXPR_A = "http://urn.fi/URN:NBN:fi:bib:expression:aaa"
EXPR_B = "http://urn.fi/URN:NBN:fi:bib:expression:bbb"
EXPR_C = "http://urn.fi/URN:NBN:fi:bib:expression:ccc"

AGENT_TOLSTOY = "http://example.org/agent/tolstoy"
AGENT_DICKENS = "http://example.org/agent/dickens"


def _records() -> dict[str, CanonicalWorkInputs]:
    return {
        WORK_A: CanonicalWorkInputs(
            work_uri=WORK_A,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Sota ja rauha",
            expression_uris=[EXPR_A],
            helmet_identifiers=[("http://example.org/ident/a1", "111")],
        ),
        WORK_B: CanonicalWorkInputs(
            work_uri=WORK_B,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Война и мир",
            expression_uris=[EXPR_B],
            helmet_identifiers=[("http://example.org/ident/b1", "222")],
        ),
        WORK_C: CanonicalWorkInputs(
            work_uri=WORK_C,
            creator_uri=AGENT_TOLSTOY,
            pref_label="War and Peace",
            expression_uris=[EXPR_C],
            helmet_identifiers=[("http://example.org/ident/c1", "333")],
        ),
    }


def _helmet_entries() -> dict[str, HelmetMapEntry]:
    return {
        WORK_A: HelmetMapEntry(WORK_A, "111", "2026-04-12T08:31:02+00:00"),
        WORK_B: HelmetMapEntry(WORK_B, "222", "2026-04-15T09:00:00+00:00"),
        WORK_C: HelmetMapEntry(WORK_C, "333", "2026-04-20T10:00:00+00:00"),
    }


def _decision_row(
    a: str,
    b: str,
    *,
    decision: str = "same_work",
    confidence: float = 0.95,
    used_cascade: bool = False,
    primary_model: str = "qwen3:32b-q4_K_M",
    fallback_model: str = "qwen2.5:72b-instruct-q4_K_M",
) -> dict[str, Any]:
    cascade: list[dict[str, Any]] = [
        {
            "stage": "llm-judge-primary",
            "model": primary_model,
            "decision": decision if not used_cascade else "uncertain",
            "confidence": confidence if not used_cascade else 0.5,
            "cache_hit": False,
            "latency_seconds": 1.0,
        }
    ]
    if used_cascade:
        cascade.append(
            {
                "stage": "llm-judge-second-opinion",
                "model": fallback_model,
                "decision": decision,
                "confidence": confidence,
                "cache_hit": False,
                "latency_seconds": 2.0,
            }
        )
    return {
        "work_a": a,
        "work_b": b,
        "similarity": 0.84,
        "block_a": "blk-1",
        "block_b": "blk-2",
        "cross_block": True,
        "decision": decision,
        "confidence": confidence,
        "rationale": "Plenty of detail here, more than twenty characters total.",
        "matching_fields": ["creator", "original_language"] if decision == "same_work" else [],
        "diverging_fields": ["preferred_title"]
        if decision == "same_work"
        else ["creator", "content_type"],
        "used_cascade": used_cascade,
        "cascade": cascade,
    }


def _write_decisions(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _run(
    tmp_path: Path,
    decisions: list[dict[str, Any]],
    *,
    work_records: dict[str, CanonicalWorkInputs] | None = None,
    helmet_entries: dict[str, HelmetMapEntry] | None = None,
    now: datetime | None = None,
) -> tuple[Path, Path, Path]:
    decisions_path = tmp_path / "judge-decisions.jsonl"
    _write_decisions(decisions_path, decisions)
    canonical = tmp_path / "canonical.ttl"
    map_path = tmp_path / "canonical-map.jsonl"
    conflicts = tmp_path / "canonical-conflicts.jsonl"
    apply_merge(
        decisions_path,
        tmp_path,
        output_path=canonical,
        map_path=map_path,
        conflicts_path=conflicts,
        helmet_map_path=tmp_path / "helmet-map.jsonl",
        work_records=work_records or _records(),
        helmet_entries=helmet_entries or _helmet_entries(),
        now=now or datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    return canonical, map_path, conflicts


# --- Chain merging --------------------------------------------------------


def test_chain_merging_combines_three_records_via_transitivity(tmp_path: Path) -> None:
    """A=B and B=C should fold all three Works into one canonical."""
    _, map_path, _ = _run(
        tmp_path,
        [
            _decision_row(WORK_A, WORK_B, decision="same_work"),
            _decision_row(WORK_B, WORK_C, decision="same_work"),
        ],
    )
    rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    entry = rows[0]
    assert sorted(entry["raw_work_uris"]) == sorted([WORK_A, WORK_B, WORK_C])
    assert sorted(entry["helmet_bib_ids"]) == ["111", "222", "333"]


def test_singleton_records_each_get_their_own_canonical(tmp_path: Path) -> None:
    """A!=B and no other edges → 3 separate canonical Works (one per record)."""
    _, map_path, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
    )
    rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 3
    assert {tuple(r["raw_work_uris"]) for r in rows} == {(WORK_A,), (WORK_B,), (WORK_C,)}


# --- Conflict handling ----------------------------------------------------


def test_conflict_a_eq_b_a_neq_c_b_eq_c_is_flagged(tmp_path: Path) -> None:
    """A=B + A!=C + B=C: the three Works land in one same_work group via transitivity,
    but the explicit different_work edge contradicts that. The whole group must
    be flagged for review and excluded from canonical.ttl / canonical-map.jsonl."""
    _, map_path, conflicts = _run(
        tmp_path,
        [
            _decision_row(WORK_A, WORK_B, decision="same_work"),
            _decision_row(WORK_A, WORK_C, decision="different_work"),
            _decision_row(WORK_B, WORK_C, decision="same_work"),
        ],
    )
    map_rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    assert map_rows == []  # no canonical Works minted for the conflicting group
    conflict_rows = [
        json.loads(line) for line in conflicts.read_text().splitlines() if line.strip()
    ]
    assert len(conflict_rows) == 1
    assert sorted(conflict_rows[0]["members"]) == sorted([WORK_A, WORK_B, WORK_C])
    assert sorted(conflict_rows[0]["conflicting_pair"]) == sorted([WORK_A, WORK_C])


# --- Identifier accumulation ---------------------------------------------


def test_canonical_carries_one_identified_by_per_absorbed_record(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonicals = list(g.subjects(V.RDF.type, V.BFFI.Work))
    merged = next(c for c in canonicals if len(list(g.objects(c, V.BF.identifiedBy))) > 1)
    idents = list(g.objects(merged, V.BF.identifiedBy))
    assert len(idents) == 2  # one per absorbed Helmet record
    bib_ids = sorted(
        str(o) for ident in idents for _, _, o in g.triples((ident, V.RDF.value, None))
    )
    assert bib_ids == ["111", "222"]


def test_identifiers_deduplicate_when_the_same_bib_id_appears_twice(tmp_path: Path) -> None:
    """If a record was somehow absorbed twice, only one bf:identifiedBy survives."""
    records = _records()
    # Make WORK_B carry the SAME bib_id as WORK_A on top of its own.
    records[WORK_B] = CanonicalWorkInputs(
        work_uri=WORK_B,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Война и мир",
        expression_uris=[EXPR_B],
        helmet_identifiers=[
            ("http://example.org/ident/b1", "222"),
            ("http://example.org/ident/a-dup", "111"),
        ],
    )
    _, map_path, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=records,
    )
    rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    entry = next(r for r in rows if WORK_A in r["raw_work_uris"])
    assert sorted(entry["helmet_bib_ids"]) == ["111", "222"]


# --- Idempotency ----------------------------------------------------------


def test_running_merge_twice_produces_byte_identical_outputs(tmp_path: Path) -> None:
    fixed = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    canonical1, map1, conflicts1 = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        now=fixed,
    )
    bytes_canonical_a = canonical1.read_bytes()
    bytes_map_a = map1.read_bytes()
    bytes_conflicts_a = conflicts1.read_bytes()

    canonical2, map2, conflicts2 = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        now=fixed,
    )
    assert canonical2.read_bytes() == bytes_canonical_a
    assert map2.read_bytes() == bytes_map_a
    assert conflicts2.read_bytes() == bytes_conflicts_a


# --- AdminMetadata --------------------------------------------------------


def _admin_block(g: Graph, canonical: URIRef) -> URIRef:
    blocks = list(g.objects(canonical, V.adminMetadata))
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, URIRef)
    return block


def test_canonical_work_carries_one_admin_metadata_block_with_required_predicates(
    tmp_path: Path,
) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = next(g.subjects(V.RDF.type, V.BFFI.Work))
    block = _admin_block(g, canonical)

    expected_predicates = {
        V.adminMetadataFor,
        V.descriptionCreationDate,
        V.descriptionChangeDate,
        V.dateGenerated,
        V.descriptionModifier,
        V.descriptionConventions,
        V.descriptionLevel,
        V.encodingLevel,
        V.descriptionAuthentication,
        V.generationProcess,
        V.metadataLicensor,
        V.recordingSource,
        V.sourceMetadata,
    }
    seen = {p for _, p, _ in g.triples((block, None, None))}
    assert expected_predicates <= seen


def test_admin_metadata_source_metadata_count_matches_absorbed_records(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [
            _decision_row(WORK_A, WORK_B, decision="same_work"),
            _decision_row(WORK_B, WORK_C, decision="same_work"),
        ],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = next(g.subjects(V.RDF.type, V.BFFI.Work))
    block = _admin_block(g, canonical)
    sources = list(g.objects(block, V.sourceMetadata))
    assert len(sources) == 3  # one per absorbed Helmet record


def test_singleton_admin_metadata_modifier_is_marc2bibframe2_agent(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],  # all singletons
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonicals = list(g.subjects(V.RDF.type, V.BFFI.Work))
    for c in canonicals:
        block = _admin_block(g, c)
        modifiers = set(g.objects(block, V.descriptionModifier))
        assert V.AGENT_MARC2BIBFRAME2 in modifiers


def test_merged_admin_metadata_modifier_is_cascade_winning_agent(tmp_path: Path) -> None:
    """A cascade-resolved same_work picks the second-opinion model URI as the modifier."""
    canonical_path, _, _ = _run(
        tmp_path,
        [
            _decision_row(
                WORK_A,
                WORK_B,
                decision="same_work",
                used_cascade=True,
                fallback_model="qwen2.5:72b-instruct-q4_K_M",
            )
        ],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonicals = list(g.subjects(V.RDF.type, V.BFFI.Work))
    # The merged Work has 2 absorbed; the singleton C is also present.
    merged = next(c for c in canonicals if len(list(g.objects(c, V.BF.identifiedBy))) > 1)
    block = _admin_block(g, merged)
    modifiers = {str(o) for o in g.objects(block, V.descriptionModifier)}
    assert any("qwen2.5-72b-instruct" in m for m in modifiers)


# --- Expression rewriting ------------------------------------------------


def test_canonical_has_expressions_and_expression_points_back_at_canonical(
    tmp_path: Path,
) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = next(
        c
        for c in g.subjects(V.RDF.type, V.BFFI.Work)
        if len(list(g.objects(c, V.BF.identifiedBy))) > 1
    )
    exprs = sorted(str(o) for o in g.objects(canonical, V.BFFI.hasExpression))
    assert exprs == sorted([EXPR_A, EXPR_B])
    # Each expression's bffi:expressionOf points back at the canonical (rewritten).
    for expr in (URIRef(EXPR_A), URIRef(EXPR_B)):
        targets = set(g.objects(expr, V.BFFI.expressionOf))
        assert canonical in targets


def test_canonical_has_was_derived_from_links_to_each_raw_work(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = next(
        c
        for c in g.subjects(V.RDF.type, V.BFFI.Work)
        if len(list(g.objects(c, V.BF.identifiedBy))) > 1
    )
    derived = sorted(str(o) for o in g.objects(canonical, V.PROV.wasDerivedFrom))
    assert derived == sorted([WORK_A, WORK_B])


# --- Pre-conditions / failure modes --------------------------------------


def test_missing_decisions_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        apply_merge(
            tmp_path / "missing.jsonl",
            tmp_path,
            work_records=_records(),
            helmet_entries=_helmet_entries(),
        )


def test_decisions_with_uncertain_are_counted_but_dont_merge(tmp_path: Path) -> None:
    """uncertain decisions don't drive a merge nor flag a conflict."""
    _, map_path, conflicts = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="uncertain", confidence=0.5)],
    )
    rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    # 3 singletons, no merge, no conflicts.
    assert len(rows) == 3
    assert conflicts.read_text().strip() == ""


# --- Subject + genreForm propagation (M8 extension for M9 phase 3) -------


YSO_TAMPERE = "http://www.yso.fi/onto/yso/p105076"
YSO_HELSINKI = "http://www.yso.fi/onto/yso/p105080"


def _records_with_subjects() -> dict[str, CanonicalWorkInputs]:
    """Two members of one merge group, each carrying overlapping subjects."""
    return {
        WORK_A: CanonicalWorkInputs(
            work_uri=WORK_A,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Sota ja rauha",
            expression_uris=[EXPR_A],
            helmet_identifiers=[("http://example.org/ident/a1", "111")],
            subject_targets=[
                SubjectTarget(uri=YSO_TAMPERE),
                SubjectTarget(label="Sota", source="yso/fin"),
            ],
            genre_form_targets=[
                SubjectTarget(label="historialliset romaanit", source="kauno/fin"),
            ],
        ),
        WORK_B: CanonicalWorkInputs(
            work_uri=WORK_B,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Война и мир",
            expression_uris=[EXPR_B],
            helmet_identifiers=[("http://example.org/ident/b1", "222")],
            subject_targets=[
                SubjectTarget(uri=YSO_TAMPERE),  # duplicate URI across members
                SubjectTarget(uri=YSO_HELSINKI),  # only on B
                SubjectTarget(label="Sota", source="yso/fin"),  # duplicate blank-node key
            ],
            genre_form_targets=[
                SubjectTarget(label="sotaromaanit", source="kauno/fin"),
            ],
        ),
        WORK_C: CanonicalWorkInputs(  # singleton — gets its own canonical
            work_uri=WORK_C,
            creator_uri=AGENT_TOLSTOY,
            pref_label="War and Peace",
            expression_uris=[EXPR_C],
            helmet_identifiers=[("http://example.org/ident/c1", "333")],
        ),
    }


def _merged_canonical(g: Graph) -> URIRef:
    """The merged-group canonical (the one with > 1 bf:identifiedBy)."""
    for c in g.subjects(V.RDF.type, V.BFFI.Work):
        if isinstance(c, URIRef) and len(list(g.objects(c, V.BF.identifiedBy))) > 1:
            return c
    raise AssertionError("no merged canonical found")


def test_uri_subjects_propagate_and_dedupe_across_absorbed_members(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_subjects(),
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = _merged_canonical(g)
    subjects = sorted(str(o) for o in g.objects(canonical, V.BFFI.subject) if isinstance(o, URIRef))
    # YSO_TAMPERE is shared (A and B) and dedupes to one triple; YSO_HELSINKI
    # only appears on B but still propagates.
    assert subjects == sorted([YSO_TAMPERE, YSO_HELSINKI])


def test_blank_node_subjects_propagate_with_label_and_source(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_subjects(),
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = _merged_canonical(g)
    blank_targets = [o for o in g.objects(canonical, V.BFFI.subject) if not isinstance(o, URIRef)]
    # Both A and B carry the same ("Sota", "yso/fin") blank-node key →
    # one blank node on the canonical, not two.
    assert len(blank_targets) == 1
    target = blank_targets[0]
    labels = {str(o) for o in g.objects(target, V.RDFS.label)}
    sources = {str(o) for o in g.objects(target, V.BF.source)}
    assert labels == {"Sota"}
    assert sources == {"yso/fin"}


def test_genre_form_propagation_uses_genre_form_predicate(tmp_path: Path) -> None:
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_subjects(),
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = _merged_canonical(g)
    # Both members contribute one distinct genre-form blank node each.
    genre_forms = list(g.objects(canonical, V.BFFI.genreForm))
    assert len(genre_forms) == 2
    labels = sorted(
        str(label) for target in genre_forms for label in g.objects(target, V.RDFS.label)
    )
    assert labels == ["historialliset romaanit", "sotaromaanit"]


def test_subject_propagation_byte_stable_across_runs(tmp_path: Path) -> None:
    """The blank-node ordering is deterministic so canonical.ttl is byte-stable."""
    fixed = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    canonical1, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_subjects(),
        now=fixed,
    )
    bytes_1 = canonical1.read_bytes()
    canonical2, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_subjects(),
        now=fixed,
    )
    assert canonical2.read_bytes() == bytes_1

"""Unit tests for P-06 sibling: gold-set growth for the M3 contrib-extract cascade.

Pure-function tests + stub-extractor-driven generate_candidates tests
so the suite stays langchain-free (no ``requires_llm`` marks).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.contrib_extract import ExtractionInputs
from bffi_pipeline.contrib_extract_llm import (
    ContribCandidate,
    ContribExtractDecision,
    StubContribExtractor,
)
from bffi_pipeline.eval.grow_contrib import (
    ADDED_BY,
    _load_existing_vetted_ids,
    _suggest_category,
    candidates_from_iterable,
    generate_candidates,
)
from bffi_pipeline.provenance import vocab as V

# --- Category suggestion -------------------------------------------------


class TestCategorySuggestion:
    """The auto-suggested category is a hint; cataloguer overrides on
    merge. We pin the suggestion logic so future changes are explicit."""

    def test_all_transliteration_suggests_transliteration_category(self) -> None:
        assert (
            _suggest_category(
                [
                    {"name": "X", "transliteration_of": "Last, X"},
                    {"name": "Y", "transliteration_of": "Last, Y"},
                ]
            )
            == "transliteration"
        )

    def test_single_new_agent_suggests_role_classification(self) -> None:
        """A single new agent is usually the cataloguer asking 'what
        role does this person play?' — role-classification is the
        canonical category."""
        assert _suggest_category([{"name": "X", "relator_code": "edt"}]) == "role-classification"

    def test_multi_new_agent_suggests_pure_new_agent(self) -> None:
        assert (
            _suggest_category(
                [
                    {"name": "X", "relator_code": "edt"},
                    {"name": "Y", "relator_code": "ill"},
                ]
            )
            == "pure-new-agent"
        )

    def test_mixed_translit_and_new_suggests_ambiguous(self) -> None:
        assert (
            _suggest_category(
                [
                    {"name": "X", "transliteration_of": "Last, X"},
                    {"name": "Y", "relator_code": "edt"},
                ]
            )
            == "ambiguous-multi-shape"
        )


# --- candidates_from_iterable -------------------------------------------


def test_candidates_from_iterable_builds_full_row() -> None:
    """Pure-functional builder produces a row matching ``gold/contrib.jsonl``'s
    shape. Used in tests + when decisions come from a non-graph
    source (e.g. fixture)."""
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example/work/1"),
        c_subfield="kirjoittanut Mika Waltari",
        existing_agent_labels=("Waltari, Mika,",),
    )
    contributions: list[dict[str, Any]] = [
        {"name": "Mika Waltari", "transliteration_of": "Waltari, Mika,"}
    ]
    rows = candidates_from_iterable(
        [("b001", inputs, contributions)],
        today="2026-06-02",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.id == "cg-pending-0001"
    assert r.category == "transliteration"
    assert r.helmet_bib_id == "b001"
    assert r.c_subfield == "kirjoittanut Mika Waltari"
    assert r.existing_agents == ["Waltari, Mika,"]
    assert r.expected_contributions == contributions
    assert r.holdout is False
    assert r.added == "2026-06-02"
    assert r.added_by == ADDED_BY


def test_candidates_from_iterable_increments_sequence_id() -> None:
    inputs = ExtractionInputs(
        work_uri=URIRef("http://example/work/1"),
        c_subfield="x",
        existing_agent_labels=(),
    )
    contribs: list[dict[str, Any]] = [{"name": "X", "relator_code": "edt"}]
    rows = candidates_from_iterable(
        [("b001", inputs, contribs), ("b002", inputs, contribs), ("b003", inputs, contribs)],
        today="2026-06-02",
    )
    assert [r.id for r in rows] == [
        "cg-pending-0001",
        "cg-pending-0002",
        "cg-pending-0003",
    ]


# --- _load_existing_vetted_ids ------------------------------------------


class TestLoadVettedIds:
    def test_missing_file_returns_empty_set(self, tmp_path: Path) -> None:
        assert _load_existing_vetted_ids(tmp_path / "missing.jsonl") == set()

    def test_reads_helmet_bib_ids_from_each_row(self, tmp_path: Path) -> None:
        p = tmp_path / "contrib.jsonl"
        p.write_text(
            '{"id": "cg-0001", "helmet_bib_id": "b001", "category": "x"}\n'
            '{"id": "cg-0002", "helmet_bib_id": "b002"}\n'
            "\n"
            '{"id": "cg-0003", "helmet_bib_id": "b003"}\n',
            encoding="utf-8",
        )
        assert _load_existing_vetted_ids(p) == {"b001", "b002", "b003"}

    def test_skips_rows_without_helmet_bib_id(self, tmp_path: Path) -> None:
        p = tmp_path / "contrib.jsonl"
        p.write_text(
            '{"id": "cg-0001", "c_subfield": "no bib"}\n'
            '{"id": "cg-0002", "helmet_bib_id": "b002"}\n',
            encoding="utf-8",
        )
        assert _load_existing_vetted_ids(p) == {"b002"}


# --- generate_candidates (integration with stub extractor) --------------


def _build_fixture_bibframe(
    *,
    bib_id: str,
    c_subfield: str,
    existing_agents: tuple[str, ...] = (),
) -> Graph:
    """Build the smallest BIBFRAME graph the contrib-extract heuristic
    will accept: one bf:Work + bf:hasInstance + bf:responsibilityStatement
    + bf:contribution chain carrying the existing agent labels."""
    g = Graph()
    work = URIRef(f"http://urn.fi/URN:NBN:fi:bib:raw/{bib_id}#Work")
    instance = URIRef(f"http://urn.fi/URN:NBN:fi:bib:raw/{bib_id}#Instance")
    g.add((work, RDF.type, V.BF.Work))
    g.add((work, V.BF.hasInstance, instance))
    g.add((instance, V.BF.responsibilityStatement, Literal(c_subfield)))
    # Helmet identifier so _read_helmet_bib_id can find it.
    ident = BNode()
    g.add((work, V.BF.identifiedBy, ident))
    g.add((ident, V.BF.source, V.HELMET_SOURCE_URI))
    g.add((ident, RDF.value, Literal(bib_id)))
    for label in existing_agents:
        contrib = BNode()
        agent = BNode()
        g.add((work, V.BF.contribution, contrib))
        g.add((contrib, V.BF.agent, agent))
        g.add((agent, RDFS.label, Literal(label)))
    return g


def _write_bibframe(tmp_path: Path, bib_id: str, graph: Graph) -> Path:
    bib_dir = tmp_path / "bibframe"
    bib_dir.mkdir(parents=True, exist_ok=True)
    path = bib_dir / f"{bib_id}.rdf"
    path.write_text(graph.serialize(format="xml"), encoding="utf-8")
    return path


def test_generate_candidates_writes_one_row_per_decision(tmp_path: Path) -> None:
    """End-to-end: a fixture BIBFRAME with one work whose 245$c
    contains an uncovered token + a stub extractor that returns one
    new contribution → one candidate row in the output JSONL."""
    g = _build_fixture_bibframe(
        bib_id="b001",
        c_subfield="by Christopher Hogwood",
        existing_agents=("Vivaldi, Antonio",),
    )
    _write_bibframe(tmp_path, "b001", g)
    stub = StubContribExtractor(
        decisions={
            "by Christopher Hogwood": ContribExtractDecision(
                contributions=[
                    ContribCandidate(
                        name="Christopher Hogwood",
                        relator_code="cnd",
                    )
                ],
                rationale="Single agent introduced by 'by' — Hogwood, conductor (cnd).",
            )
        }
    )
    output = tmp_path / "candidates.jsonl"
    summary = generate_candidates(
        bibframe_dir=tmp_path / "bibframe",
        output_path=output,
        extractor=stub,
    )
    assert summary.bibframe_files_scanned == 1
    assert summary.works_with_245c == 1
    assert summary.heuristic_fires == 1
    assert summary.llm_decisions_with_contributions == 1
    assert summary.candidates_written == 1

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["helmet_bib_id"] == "b001"
    assert row["c_subfield"] == "by Christopher Hogwood"
    assert row["existing_agents"] == ["Vivaldi, Antonio"]
    assert row["expected_contributions"] == [{"name": "Christopher Hogwood", "relator_code": "cnd"}]
    assert row["holdout"] is False
    assert row["category"] == "role-classification"


def test_generate_candidates_skips_when_heuristic_does_not_fire(tmp_path: Path) -> None:
    """245$c whose tokens are all already in existing_agents → heuristic
    doesn't fire → no LLM call, no candidate row."""
    g = _build_fixture_bibframe(
        bib_id="b001",
        # Every token covered by existing_agents — heuristic skips.
        c_subfield="Vivaldi",
        existing_agents=("Vivaldi, Antonio",),
    )
    _write_bibframe(tmp_path, "b001", g)
    stub = StubContribExtractor(decisions={})
    output = tmp_path / "candidates.jsonl"
    summary = generate_candidates(
        bibframe_dir=tmp_path / "bibframe",
        output_path=output,
        extractor=stub,
    )
    assert summary.heuristic_fires == 0
    assert summary.candidates_written == 0
    assert output.read_text(encoding="utf-8") == ""


def test_generate_candidates_skips_when_no_extractor(tmp_path: Path) -> None:
    """``--no-llm-cascade`` mode: walks corpus + counts heuristic-fires
    without writing candidates. Useful for cost projection."""
    g = _build_fixture_bibframe(
        bib_id="b001",
        c_subfield="by Christopher Hogwood",
        existing_agents=("Vivaldi, Antonio",),
    )
    _write_bibframe(tmp_path, "b001", g)
    output = tmp_path / "candidates.jsonl"
    summary = generate_candidates(
        bibframe_dir=tmp_path / "bibframe",
        output_path=output,
        extractor=None,
    )
    assert summary.heuristic_fires == 1
    assert summary.candidates_written == 0
    assert output.read_text(encoding="utf-8") == ""


def test_generate_candidates_dedups_against_existing_gold(tmp_path: Path) -> None:
    """Records whose helmet_bib_id is already in gold/contrib.jsonl are
    skipped without invoking the LLM. Re-runs don't re-ask the cataloguer
    to re-vet cases they already merged."""
    g = _build_fixture_bibframe(
        bib_id="b001",
        c_subfield="by Christopher Hogwood",
        existing_agents=("Vivaldi, Antonio",),
    )
    _write_bibframe(tmp_path, "b001", g)
    gold = tmp_path / "contrib.jsonl"
    gold.write_text('{"id": "cg-0001", "helmet_bib_id": "b001"}\n', encoding="utf-8")
    stub = StubContribExtractor(
        decisions={
            "by Christopher Hogwood": ContribExtractDecision(
                contributions=[
                    ContribCandidate(name="Christopher Hogwood", relator_code="cnd"),
                ],
                rationale="should not appear in candidates because b001 is already vetted.",
            )
        }
    )
    output = tmp_path / "candidates.jsonl"
    summary = generate_candidates(
        bibframe_dir=tmp_path / "bibframe",
        output_path=output,
        extractor=stub,
        existing_gold_path=gold,
    )
    assert summary.skipped_by_existing_id == 1
    assert summary.candidates_written == 0


def test_generate_candidates_respects_limit(tmp_path: Path) -> None:
    """``--limit`` caps the number of BIBFRAME files scanned, regardless
    of how many fire the heuristic."""
    for bib_id in ("b001", "b002", "b003"):
        g = _build_fixture_bibframe(
            bib_id=bib_id,
            c_subfield="by Christopher Hogwood",
            existing_agents=("Vivaldi, Antonio",),
        )
        _write_bibframe(tmp_path, bib_id, g)
    stub = StubContribExtractor(
        decisions={
            "by Christopher Hogwood": ContribExtractDecision(
                contributions=[
                    ContribCandidate(name="Christopher Hogwood", relator_code="cnd"),
                ],
                rationale="Single agent — Hogwood, conductor (cnd).",
            )
        }
    )
    output = tmp_path / "candidates.jsonl"
    summary = generate_candidates(
        bibframe_dir=tmp_path / "bibframe",
        output_path=output,
        extractor=stub,
        limit=2,
    )
    assert summary.bibframe_files_scanned == 2
    assert summary.candidates_written == 2

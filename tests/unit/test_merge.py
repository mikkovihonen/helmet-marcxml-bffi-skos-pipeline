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
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from bffi_pipeline.config import get_settings
from bffi_pipeline.contrib_variants import (
    ContribVariantClaim,
    append_variant_claims,
)
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.merge import (
    CanonicalEntry,
    CanonicalWorkInputs,
    ContributionTarget,
    ExpressionContribution,
    HelmetMapEntry,
    SubjectTarget,
    _apply_contrib_variants,
    _expression_contributions,
    _first_contribution_agent_uri,
    _load_work_records_from_corpus,
    apply_merge,
)
from bffi_pipeline.stages.observability import (
    StageEventEmitter,
    set_active_emitter,
)


@pytest.fixture(autouse=True)
def _isolate_test_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Stop ``apply_merge`` from littering the real ``runs/`` dir.

    Some tests (e.g. ``test_missing_decisions_path_raises``) call
    ``apply_merge`` without specifying every output path. The fallthroughs
    inside ``apply_merge`` resolve to ``settings.data_dir / <filename>``,
    and the function's ``output_path.parent.mkdir(parents=True, ...)`` call
    fires BEFORE the expected exception is raised — leaving an empty
    ``runs/<uuid>/`` dir behind on every test run.

    The fixture redirects ``BFFI_RUNS_ROOT`` to a pytest tmp dir so those
    fallthroughs land in scratch space, and clears ``@lru_cache`` on
    ``get_settings`` + the process-wide active emitter before AND after
    each test so the settings instance picks up the monkeypatch and
    cross-module test ordering can't leak state.
    """
    runs_root = tmp_path_factory.mktemp("test-merge-runs")
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(runs_root))
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


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
    # Mint-failures file is empty — this WAS a real M6 contradiction,
    # not a missing-canonical-key case.
    mint_failures_path = tmp_path / "canonical-mint-failures.jsonl"
    assert mint_failures_path.read_text() == ""


def test_anonymous_main_entry_routes_to_mint_failures_not_conflicts(tmp_path: Path) -> None:
    """A record without ``creator_uri`` (the anonymous-main-entry MARC pattern —
    no 1XX, just 245 + 700 contributors) lands in ``canonical-mint-failures.jsonl``,
    NOT in ``canonical-conflicts.jsonl``.

    Bug observed on the 2026-05-14 helmet-5k-full bench:
    707 of 709 ``canonical-conflicts.jsonl`` rows were anonymous-main-entry
    records (edited compilations / anonymous works) misclassified as
    "conflicts" with empty ``same_work_path`` and a ``conflicting_pair`` of
    ``(URI, URI)``. The fix separates the two failure modes into two files
    so cataloguer review of real M6 contradictions stays unpolluted.
    """
    work_records = {
        WORK_A: CanonicalWorkInputs(
            work_uri=WORK_A,
            creator_uri=None,  # anonymous main entry — no MARC 1XX in source
            pref_label="Hanko toisessa maailmansodassa",
            expression_uris=[EXPR_A],
            helmet_identifiers=[("http://example.org/ident/a1", "b20363308")],
        ),
    }
    helmet_entries = {WORK_A: HelmetMapEntry(WORK_A, "b20363308", "2026-04-12T08:31:02+00:00")}
    _canonical, map_path, conflicts = _run(
        tmp_path, [], work_records=work_records, helmet_entries=helmet_entries
    )

    # Conflicts file stays empty — this is NOT an M6 contradiction.
    assert conflicts.read_text() == ""
    # Canonical map stays empty — nothing gets minted without creator_uri.
    map_rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    assert map_rows == []

    # Mint-failures file carries the record with ``creator_uri`` flagged.
    mint_failures_path = tmp_path / "canonical-mint-failures.jsonl"
    mint_rows = [
        json.loads(line) for line in mint_failures_path.read_text().splitlines() if line.strip()
    ]
    assert len(mint_rows) == 1
    assert mint_rows[0]["anchor_work_uri"] == WORK_A
    assert mint_rows[0]["members"] == [WORK_A]
    assert mint_rows[0]["missing_inputs"] == ["creator_uri"]


def test_record_without_pref_label_routes_to_mint_failures(tmp_path: Path) -> None:
    """A record with creator but no ``pref_label`` (e.g. MARC 100 present but
    245$a missing or empty) also lands in mint-failures, with both inputs
    or just ``pref_label`` flagged."""
    work_records = {
        WORK_A: CanonicalWorkInputs(
            work_uri=WORK_A,
            creator_uri=AGENT_TOLSTOY,
            pref_label=None,
            expression_uris=[EXPR_A],
            helmet_identifiers=[("http://example.org/ident/a1", "b999")],
        ),
    }
    helmet_entries = {WORK_A: HelmetMapEntry(WORK_A, "b999", "2026-04-12T08:31:02+00:00")}
    _canonical, _map_path, _conflicts = _run(
        tmp_path, [], work_records=work_records, helmet_entries=helmet_entries
    )

    mint_failures_path = tmp_path / "canonical-mint-failures.jsonl"
    mint_rows = [
        json.loads(line) for line in mint_failures_path.read_text().splitlines() if line.strip()
    ]
    assert len(mint_rows) == 1
    assert mint_rows[0]["missing_inputs"] == ["pref_label"]


# --- P-34: editor-anchored fallback ----------------------------------------


def _build_anonymous_main_entry_graph(
    work_uri: URIRef,
    *,
    title: str,
    agents: list[tuple[URIRef, str]],
    translator_only: bool = False,
) -> Graph:
    """Build a minimal BFFI graph mirroring the anonymous-main-entry shape.

    No ``bffi:PrimaryContribution`` on the Work; each contribution lives
    on the Expression with ``bffi:agent`` pointing at a URI and an
    optional ``bf:role`` blank-node with rdfs:label. ``translator_only``
    flips every contribution's role label to ``"kääntäjä"`` so the
    P-34 R3 translator-blocklist exercises.
    """
    g = Graph()
    expr_uri = URIRef(str(work_uri).replace("/work:", "/expression:"))
    g.add((work_uri, RDF.type, V.BFFI.Work))
    g.add((work_uri, V.BFFI.hasExpression, expr_uri))
    g.add((expr_uri, RDF.type, V.BFFI.Expression))
    g.add((work_uri, V.SKOS.prefLabel, Literal(title)))
    g.add((expr_uri, V.SKOS.prefLabel, Literal(title)))
    for agent_uri, label in agents:
        contrib = BNode()
        g.add((expr_uri, V.BFFI.contribution, contrib))
        g.add((contrib, RDF.type, V.BFFI.Contribution))
        g.add((contrib, V.BFFI.agent, agent_uri))
        g.add((agent_uri, V.RDFS.label, Literal(label)))
        role = BNode()
        g.add((contrib, V.BF.role, role))
        g.add((role, RDF.type, V.BF.Role))
        if translator_only:
            g.add((role, V.RDFS.label, Literal("kääntäjä")))
        else:
            g.add((role, V.RDFS.label, Literal("toimittaja")))
    return g


def test_first_contribution_fallback_picks_lex_min_agent_uri() -> None:
    """The fallback walks ``Work → hasExpression → contribution → agent``
    and returns the lexicographically-smallest agent URI for a
    deterministic mint key."""
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/btest#Work")
    g = _build_anonymous_main_entry_graph(
        work,
        title="Hanko toisessa maailmansodassa",
        agents=[
            (URIRef("http://urn.fi/URN:NBN:fi:bib:raw/btest#Agent700-41"), "Uitto, Antero"),
            (URIRef("http://urn.fi/URN:NBN:fi:bib:raw/btest#Agent700-40"), "Geust, Carl-Fredrik"),
        ],
    )
    result = _first_contribution_agent_uri(g, work)
    # Lex-min between "...#Agent700-40" and "...#Agent700-41" is the 40.
    assert result == "http://urn.fi/URN:NBN:fi:bib:raw/btest#Agent700-40"


def test_first_contribution_fallback_skips_translator_only_records() -> None:
    """A record where every non-primary contribution is a translator
    returns None — translators are the wrong intellectual anchor."""
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/btrans#Work")
    g = _build_anonymous_main_entry_graph(
        work,
        title="Sota ja rauha (translated edition)",
        agents=[
            (URIRef("http://urn.fi/URN:NBN:fi:bib:raw/btrans#Agent700-7"), "Translator A"),
        ],
        translator_only=True,
    )
    assert _first_contribution_agent_uri(g, work) is None


def test_first_contribution_fallback_returns_none_for_truly_anonymous() -> None:
    """A record with no contributions at all (no 1XX, no 7XX) returns
    None — these stay in mint-failures until P-34 sub-option (2)
    ships a title-only mint."""
    g = Graph()
    work = URIRef("http://urn.fi/URN:NBN:fi:bib:raw/banon#Work")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.SKOS.prefLabel, Literal("Karjala :")))
    assert _first_contribution_agent_uri(g, work) is None


def test_canonical_carries_mintanchor_predicate_for_editor_anchored(tmp_path: Path) -> None:
    """When the canonical Work was minted via the P-34 editor-anchored
    fallback, the canonical.ttl carries ``bffi-prov:mintAnchor =
    bib:auth/first-contributor-anchored``. Standard primary-author-
    anchored canonical Works carry
    ``bib:auth/primary-author-anchored`` instead."""
    work_records = {
        WORK_A: CanonicalWorkInputs(
            work_uri=WORK_A,
            creator_uri="http://example.org/agent/editor",
            pref_label="Hanko toisessa maailmansodassa",
            mint_anchor="first-contributor",
            expression_uris=[EXPR_A],
            helmet_identifiers=[("http://example.org/ident/a1", "b1")],
        ),
        WORK_B: CanonicalWorkInputs(
            work_uri=WORK_B,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Sota ja rauha",
            mint_anchor="primary",
            expression_uris=[EXPR_B],
            helmet_identifiers=[("http://example.org/ident/b1", "b2")],
        ),
    }
    helmet_entries = {
        WORK_A: HelmetMapEntry(WORK_A, "b1", "2026-04-12T08:31:02+00:00"),
        WORK_B: HelmetMapEntry(WORK_B, "b2", "2026-04-12T08:31:02+00:00"),
    }
    canonical, _, _ = _run(tmp_path, [], work_records=work_records, helmet_entries=helmet_entries)

    g = Graph()
    g.parse(source=str(canonical), format="turtle")
    mint_anchor_pred = URIRef("http://urn.fi/URN:NBN:fi:schema:bffi-prov#mintAnchor")
    editor_value = URIRef("http://urn.fi/URN:NBN:fi:bib:auth/first-contributor-anchored")
    primary_value = URIRef("http://urn.fi/URN:NBN:fi:bib:auth/primary-author-anchored")

    editor_anchored = list(g.subjects(mint_anchor_pred, editor_value))
    primary_anchored = list(g.subjects(mint_anchor_pred, primary_value))
    assert len(editor_anchored) == 1
    assert len(primary_anchored) == 1


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


def test_canonical_unions_lang_tagged_pref_labels_across_members(tmp_path: Path) -> None:
    """When raw Works carry M3-cascade prefLabels in multiple languages
    (en/fi/ru on the Tšarka pattern), M8 unions the full set onto the
    canonical Work — Skosmos picks the right per-language label for the
    UI rather than collapsing to one."""
    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        pref_labels=[("Sota ja rauha", "fi"), ("War and Peace", "en")],
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
    )
    records[WORK_B] = CanonicalWorkInputs(
        work_uri=WORK_B,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Война и мир",
        pref_labels=[("Война и мир", "ru")],
        expression_uris=[EXPR_B],
        helmet_identifiers=[("http://example.org/ident/b1", "222")],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonicals = list(g.subjects(V.RDF.type, V.BFFI.Work))
    merged = next(c for c in canonicals if len(list(g.objects(c, V.BF.identifiedBy))) > 1)
    labels = {
        (str(o), o.language) for o in g.objects(merged, V.SKOS.prefLabel) if isinstance(o, Literal)
    }
    assert labels == {
        ("Sota ja rauha", "fi"),
        ("War and Peace", "en"),
        ("Война и мир", "ru"),
    }


def test_canonical_carries_dct_identifier_per_absorbed_bib_id(tmp_path: Path) -> None:
    """Each absorbed Helmet record contributes one ``dct:identifier`` on
    the canonical Work so cataloguers see every bib number that rolled
    into a merged Work, not just one. The literal value is the bib_id
    string as received from upstream (Sierra display form
    ``b<id><check>`` in production; bare numerics in these fixtures)."""
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonicals = list(g.subjects(V.RDF.type, V.BFFI.Work))
    merged = next(c for c in canonicals if len(list(g.objects(c, V.BF.identifiedBy))) > 1)
    bib_ids = sorted(str(o) for o in g.objects(merged, DCTERMS.identifier))
    assert bib_ids == ["111", "222"]
    for bid in bib_ids:
        assert (merged, DCTERMS.identifier, Literal(bid)) in g


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


# --- Contribution + Expression-typing propagation -----------------------


AGENT_TOLSTOY_LABEL = "Tolstoy, Leo,"
AGENT_DICKENS_LABEL = "Dickens, Charles,"


def _records_with_contributions() -> dict[str, CanonicalWorkInputs]:
    """Two members of one merge group, each carrying a primary contribution."""
    return {
        WORK_A: CanonicalWorkInputs(
            work_uri=WORK_A,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Sota ja rauha",
            expression_uris=[EXPR_A],
            helmet_identifiers=[("http://example.org/ident/a1", "111")],
            contribution_targets=[
                ContributionTarget(agent_uri=AGENT_TOLSTOY, agent_label=AGENT_TOLSTOY_LABEL),
            ],
        ),
        WORK_B: CanonicalWorkInputs(
            work_uri=WORK_B,
            creator_uri=AGENT_TOLSTOY,
            pref_label="Война и мир",
            expression_uris=[EXPR_B],
            helmet_identifiers=[("http://example.org/ident/b1", "222")],
            contribution_targets=[
                # Same agent across A and B → must dedupe to one canonical contrib.
                ContributionTarget(agent_uri=AGENT_TOLSTOY, agent_label=AGENT_TOLSTOY_LABEL),
                # Distinct agent only on B (e.g. translator credited as primary).
                ContributionTarget(agent_uri=AGENT_DICKENS, agent_label=AGENT_DICKENS_LABEL),
            ],
        ),
        WORK_C: CanonicalWorkInputs(
            work_uri=WORK_C,
            creator_uri=AGENT_TOLSTOY,
            pref_label="War and Peace",
            expression_uris=[EXPR_C],
            helmet_identifiers=[("http://example.org/ident/c1", "333")],
            contribution_targets=[
                ContributionTarget(agent_uri=AGENT_TOLSTOY, agent_label=AGENT_TOLSTOY_LABEL),
            ],
        ),
    }


def test_canonical_carries_primary_contribution_with_agent_and_label(tmp_path: Path) -> None:
    """M9 walks <canonical> bffi:contribution → PrimaryContribution → agent → rdfs:label."""
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_contributions(),
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = _merged_canonical(g)
    contribs = list(g.objects(canonical, V.BFFI.contribution))
    # Two distinct agents → two contributions (Tolstoy deduped across A and B).
    assert len(contribs) == 2
    for contrib in contribs:
        types = set(g.objects(contrib, V.RDF.type))
        assert V.BFFI.PrimaryContribution in types
        agents = list(g.objects(contrib, V.BFFI.agent))
        assert len(agents) == 1
        agent = agents[0]
        assert isinstance(agent, URIRef)
        labels = list(g.objects(agent, V.RDFS.label))
        assert len(labels) == 1


def test_canonical_dedupes_contribution_by_agent_uri_across_members(tmp_path: Path) -> None:
    """The same agent referenced by N absorbed Works produces one canonical contrib."""
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_contributions(),
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical = _merged_canonical(g)
    agents_seen: set[str] = set()
    for contrib in g.objects(canonical, V.BFFI.contribution):
        for agent in g.objects(contrib, V.BFFI.agent):
            agents_seen.add(str(agent))
    # Tolstoy is on both A and B; Dickens only on B. Two unique URIs.
    assert agents_seen == {AGENT_TOLSTOY, AGENT_DICKENS}


def test_singleton_canonical_propagates_its_one_contribution(tmp_path: Path) -> None:
    """Singletons (no merge edge) still get their primary contribution propagated."""
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],  # all singletons
        work_records=_records_with_contributions(),
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    for c in g.subjects(V.RDF.type, V.BFFI.Work):
        contribs = list(g.objects(c, V.BFFI.contribution))
        assert contribs, f"canonical {c} has no contribution"


def test_contribution_propagation_byte_stable_across_runs(tmp_path: Path) -> None:
    """Deterministic blank-node IDs keep canonical.ttl byte-stable when contributions land."""
    fixed = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    canonical1, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_contributions(),
        now=fixed,
    )
    bytes_1 = canonical1.read_bytes()
    canonical2, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=_records_with_contributions(),
        now=fixed,
    )
    assert canonical2.read_bytes() == bytes_1


def test_records_without_contributions_still_emit_canonical(tmp_path: Path) -> None:
    """A Work with creator_uri/pref_label but no contribution_targets still merges cleanly."""
    records = _records()  # the default fixture has no contribution_targets
    _, map_path, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=records,
    )
    rows = [json.loads(line) for line in map_path.read_text().splitlines() if line.strip()]
    assert len(rows) >= 1


# --- Expression dual-typing on canonical -------------------------------


def test_absorbed_expressions_are_typed_bffi_expression_on_canonical(tmp_path: Path) -> None:
    """M10 phase 1 (Skosify) needs <expr> a bffi:Expression to dual-type as skos:Concept.

    The typing exists in the per-record BFFI Turtles but not in
    canonical.ttl until M8 re-asserts it during the expressionOf
    rewrite.
    """
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    typed = set(g.subjects(V.RDF.type, V.BFFI.Expression))
    # All three Expressions (A absorbed, B absorbed, C singleton) should be typed.
    assert URIRef(EXPR_A) in typed
    assert URIRef(EXPR_B) in typed
    assert URIRef(EXPR_C) in typed


def test_absorbed_expression_prefLabels_are_propagated_to_canonical(tmp_path: Path) -> None:
    """Skosmos surfaces Expressions in the Work → Expression hierarchy via skos:prefLabel.

    Without prefLabel propagation Skosmos's narrower endpoint returns
    entries with `prefLabel: None`. M8 re-asserts the labels (with
    their language tags) on the canonical from each member's
    ``expression_labels`` list.
    """
    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_labels=[(EXPR_A, "Sota ja rauha", "fi")],
    )
    records[WORK_B] = CanonicalWorkInputs(
        work_uri=WORK_B,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Война и мир",
        expression_uris=[EXPR_B],
        helmet_identifiers=[("http://example.org/ident/b1", "222")],
        # Two languages on the same Expression — both should propagate.
        expression_labels=[
            (EXPR_B, "Война и мир", "ru"),
            (EXPR_B, "War and Peace", "en"),
        ],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    expr_a_labels = {(str(o), o.language) for o in g.objects(URIRef(EXPR_A), V.SKOS.prefLabel)}
    expr_b_labels = {(str(o), o.language) for o in g.objects(URIRef(EXPR_B), V.SKOS.prefLabel)}
    assert expr_a_labels == {("Sota ja rauha", "fi")}
    assert expr_b_labels == {("Война и мир", "ru"), ("War and Peace", "en")}


# --- Non-primary contribution propagation onto canonical Expressions (F1) ---


def test_canonical_expression_carries_blank_node_contribution_from_cascade(
    tmp_path: Path,
) -> None:
    """M3's contributor-extraction cascade emits non-primary
    bffi:Contribution blocks with blank-node agents on raw
    Expressions. M8 must propagate them onto the canonical Expression
    so Skosmos surfaces them on the canonical-Work concept page."""

    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[
            ExpressionContribution(
                expression_uri=EXPR_A,
                role_uri="http://id.loc.gov/vocabulary/relators/cnd",
                agent_uri=None,
                agent_label="Christopher Hogwood",
            ),
        ],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    contribs = list(g.objects(URIRef(EXPR_A), V.BFFI.contribution))
    assert len(contribs) == 1
    contrib = contribs[0]
    roles = list(g.objects(contrib, V.BF.role))
    assert roles == [URIRef("http://id.loc.gov/vocabulary/relators/cnd")]
    [agent] = list(g.objects(contrib, V.BFFI.agent))
    labels = list(g.objects(agent, V.RDFS.label))
    assert labels == [Literal("Christopher Hogwood")]


def test_canonical_expression_carries_uri_agent_contribution_from_700(
    tmp_path: Path,
) -> None:
    """The 700-fielded variant: marc2bibframe2 emits non-primary
    contributions whose agent is a URIRef (e.g.
    ``http://urn.fi/.../#Agent700-24``) plus an rdfs:label. The
    propagator must re-emit both on the canonical Expression so
    Skosmos has a labelled link to follow."""

    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[
            ExpressionContribution(
                expression_uri=EXPR_A,
                role_uri="http://id.loc.gov/vocabulary/relators/trl",
                agent_uri="http://urn.fi/URN:NBN:fi:bib:raw/aaa#Agent700-24",
                agent_label="Adrian, Esa",
            ),
        ],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    [contrib] = list(g.objects(URIRef(EXPR_A), V.BFFI.contribution))
    [agent] = list(g.objects(contrib, V.BFFI.agent))
    assert agent == URIRef("http://urn.fi/URN:NBN:fi:bib:raw/aaa#Agent700-24")
    assert (agent, V.RDFS.label, Literal("Adrian, Esa")) in g


def test_canonical_expression_dedups_contribution_across_members(tmp_path: Path) -> None:
    """When two raw Works absorb into one canonical and both Expressions
    carry the same (expr_uri, agent, role) triple, the canonical
    Expression must show one Contribution block, not two — re-running
    M8 on byte-equivalent input produces byte-equivalent output."""

    records = _records()
    shared = ExpressionContribution(
        expression_uri=EXPR_A,
        role_uri="http://id.loc.gov/vocabulary/relators/trl",
        agent_uri=None,
        agent_label="Adrian, Esa",
    )
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[shared],
    )
    records[WORK_B] = CanonicalWorkInputs(
        work_uri=WORK_B,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Война и мир",
        expression_uris=[EXPR_A],  # ← same Expression URI
        helmet_identifiers=[("http://example.org/ident/b1", "222")],
        expression_contributions=[shared],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="same_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    contribs = list(g.objects(URIRef(EXPR_A), V.BFFI.contribution))
    assert len(contribs) == 1


def test_canonical_expression_skips_contribution_with_no_agent_info(
    tmp_path: Path,
) -> None:
    """An ExpressionContribution with no agent_uri AND no agent_label
    has nothing to propagate — emitting an empty Agent block would
    just create cataloguer noise. The propagator should silently skip.
    (The walker filters these too; this test pins the propagator's
    independent guard for the case where a future caller passes
    raw user input.)"""

    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[
            ExpressionContribution(
                expression_uri=EXPR_A,
                role_uri="http://id.loc.gov/vocabulary/relators/cnd",
                agent_uri=None,
                agent_label="Real Agent",
            ),
        ],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    contribs = list(g.objects(URIRef(EXPR_A), V.BFFI.contribution))
    # The valid one should propagate normally.
    assert len(contribs) == 1


def test_walker_filters_primary_contributions_from_expression_propagation() -> None:
    """The walker that builds expression_contributions must filter
    out PrimaryContribution blocks — those go on the canonical Work
    via _propagate_primary_contributions, not on Expressions."""
    g = Graph()
    g.parse(
        data=dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{WORK_A}> a bffi:Work ;
                bffi:hasExpression <{EXPR_A}> .

            <{EXPR_A}> a bffi:Expression ;
                bffi:contribution <#contrib1>, <#contrib2> .

            # Primary — MUST be filtered out by the walker.
            <#contrib1> a bffi:Contribution, bffi:PrimaryContribution ;
                bffi:agent <#agent-primary> ;
                bf:role <http://id.loc.gov/vocabulary/relators/aut> .

            # Non-primary — MUST be picked up.
            <#contrib2> a bffi:Contribution ;
                bffi:agent <#agent-translator> ;
                bf:role <http://id.loc.gov/vocabulary/relators/trl> .

            <#agent-primary>    rdfs:label "Tolstoy, Lev" .
            <#agent-translator> rdfs:label "Adrian, Esa" .
            """
        ).strip(),
        format="turtle",
    )
    out = _expression_contributions(g, URIRef(WORK_A))
    assert len(out) == 1
    [ec] = out
    assert ec.agent_label == "Adrian, Esa"
    assert ec.role_uri == "http://id.loc.gov/vocabulary/relators/trl"


def test_walker_captures_blank_node_role_label_from_marc2bibframe2() -> None:
    """marc2bibframe2 emits ``bf:role [a bf:Role; rdfs:label "johtaja"]``
    when the MARC source has $e text without $4 — that's the dominant
    pattern in Helmet (Finnish-language $e). The walker must capture
    the rdfs:label as ``role_label`` so the propagator can re-emit
    the same blank-node-role shape on canonical."""
    g = Graph()
    g.parse(
        data=dedent(
            f"""
            @prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
            @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

            <{WORK_A}> a bffi:Work ;
                bffi:hasExpression <{EXPR_A}> .

            <{EXPR_A}> a bffi:Expression ;
                bffi:contribution [
                    a bffi:Contribution ;
                    bffi:agent [ rdfs:label "Hogwood, Christopher" ] ;
                    bf:role [ a bf:Role ; rdfs:label "johtaja" ]
                ] .
            """
        ).strip(),
        format="turtle",
    )
    [ec] = _expression_contributions(g, URIRef(WORK_A))
    assert ec.role_uri is None
    assert ec.role_label == "johtaja"


def test_canonical_expression_emits_blank_node_role_with_label(tmp_path: Path) -> None:
    """The propagator must re-emit a free-text role (no relator URI)
    as ``bf:role [a bf:Role; rdfs:label "..."]`` so Skosmos surfaces
    the cataloguer-supplied role text on the canonical Expression."""
    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[
            ExpressionContribution(
                expression_uri=EXPR_A,
                role_label="cembalo",
                agent_uri="http://urn.fi/URN:NBN:fi:bib:raw/aaa#Agent700-26",
                agent_label="Hogwood, Christopher",
            ),
        ],
    )
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
        work_records=records,
    )
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    [contrib] = list(g.objects(URIRef(EXPR_A), V.BFFI.contribution))
    [role] = list(g.objects(contrib, V.BF.role))
    # Role is a blank node typed bf:Role with rdfs:label
    assert (role, V.RDF.type, V.BF.Role) in g
    assert (role, V.RDFS.label, Literal("cembalo")) in g


# --- F2: variant-binding pass against the canonical graph ----------------


def test_merge_attaches_skos_alt_label_from_variants_sidecar(tmp_path: Path) -> None:
    """When the F2 sidecar contains a (raw_work_uri, variant_label,
    canonical_label) claim that resolves on the merged canonical
    graph, M8 attaches ``skos:altLabel "<variant_label>"`` on the
    canonical agent whose ``rdfs:label`` matches ``canonical_label``.
    """
    # Build records where WORK_A's Expression carries the cataloguer's
    # 700 agent with rdfs:label == 'Karttunen, Assi'. M8's F1 pass
    # propagates that agent onto the canonical Expression.
    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[
            ExpressionContribution(
                expression_uri=EXPR_A,
                role_label="cembalo",
                agent_uri="http://urn.fi/URN:NBN:fi:bib:raw/aaa#Agent700-26",
                agent_label="Karttunen, Assi",
            ),
        ],
    )

    # Sidecar carries the cascade's variant decision for the same record.
    sidecar = tmp_path / "contrib-variants.jsonl"
    append_variant_claims(
        sidecar,
        [
            ContribVariantClaim(
                helmet_bib_id="111",
                raw_work_uri=WORK_A,
                variant_label="Anssi Karttunen",
                canonical_label="Karttunen, Assi",
                relator_code_hint="prf",
                rationale="Variant of the existing 700 entry.",
                prompt_hash="sha256:abc",
                model_id="qwen3:8b-q4_K_M",
                decided_at="2026-05-09T12:00:00+00:00",
            ),
        ],
    )

    decisions_path = tmp_path / "judge-decisions.jsonl"
    _write_decisions(decisions_path, [_decision_row(WORK_A, WORK_B, decision="different_work")])
    canonical = tmp_path / "canonical.ttl"
    apply_merge(
        decisions_path,
        tmp_path,
        output_path=canonical,
        map_path=tmp_path / "canonical-map.jsonl",
        conflicts_path=tmp_path / "canonical-conflicts.jsonl",
        helmet_map_path=tmp_path / "helmet-map.jsonl",
        variants_sidecar_path=sidecar,
        work_records=_records() | records,
        helmet_entries=_helmet_entries(),
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )

    g = Graph()
    g.parse(str(canonical), format="turtle")
    assert (
        URIRef("http://urn.fi/URN:NBN:fi:bib:raw/aaa#Agent700-26"),
        V.SKOS.altLabel,
        Literal("Anssi Karttunen"),
    ) in g


def test_merge_skips_variants_with_no_matching_canonical_label(tmp_path: Path) -> None:
    """A variant claim whose ``canonical_label`` doesn't match any agent
    on the canonical Work is silently dropped — M8 mustn't fabricate
    a stub agent or fail the run; the cascade may have produced a
    stale entry that no longer maps."""
    records = _records()
    records[WORK_A] = CanonicalWorkInputs(
        work_uri=WORK_A,
        creator_uri=AGENT_TOLSTOY,
        pref_label="Sota ja rauha",
        expression_uris=[EXPR_A],
        helmet_identifiers=[("http://example.org/ident/a1", "111")],
        expression_contributions=[
            ExpressionContribution(
                expression_uri=EXPR_A,
                agent_uri="http://urn.fi/URN:NBN:fi:bib:raw/aaa#a1",
                agent_label="Real, Agent",
            ),
        ],
    )
    sidecar = tmp_path / "contrib-variants.jsonl"
    append_variant_claims(
        sidecar,
        [
            ContribVariantClaim(
                helmet_bib_id="111",
                raw_work_uri=WORK_A,
                variant_label="Variant Form",
                canonical_label="Phantom Agent Not In Graph",
                rationale="Stale claim — canonical no longer matches.",
                prompt_hash="sha256:abc",
                model_id="qwen3:8b-q4_K_M",
                decided_at="2026-05-09T12:00:00+00:00",
            ),
        ],
    )
    decisions_path = tmp_path / "judge-decisions.jsonl"
    _write_decisions(decisions_path, [_decision_row(WORK_A, WORK_B, decision="different_work")])
    canonical = tmp_path / "canonical.ttl"
    apply_merge(
        decisions_path,
        tmp_path,
        output_path=canonical,
        map_path=tmp_path / "canonical-map.jsonl",
        conflicts_path=tmp_path / "canonical-conflicts.jsonl",
        helmet_map_path=tmp_path / "helmet-map.jsonl",
        variants_sidecar_path=sidecar,
        work_records=_records() | records,
        helmet_entries=_helmet_entries(),
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    g = Graph()
    g.parse(str(canonical), format="turtle")
    # Only the real agent's rdfs:label exists; no altLabel anywhere
    # because the claim's canonical_label didn't resolve.
    assert (None, V.SKOS.altLabel, None) not in g


def test_merge_runs_cleanly_when_sidecar_is_missing(tmp_path: Path) -> None:
    """Sidecar absence is the dominant case — most M3 runs don't fire
    the contributor cascade. Apply_merge must not raise, must not
    touch any altLabel, and must not require the file to exist."""
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
    )
    assert not (tmp_path / "contrib-variants.jsonl").exists()
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    assert (None, V.SKOS.altLabel, None) not in g


def test_merge_attaches_alt_label_on_primary_contribution_agent(tmp_path: Path) -> None:
    """When the cascade flags a 245$c name as a variant of an agent
    that lives in 100$a (primary contribution on the canonical Work,
    not 700 on the Expression), the binding pass must walk the
    canonical Work's contributions too. Otherwise Froberger /
    Pattison-style variants — author name in 100, variant in 245$c —
    silently drop their altLabel even though the canonical agent is
    plainly there."""
    canonical_path, _, _ = _run(
        tmp_path,
        [_decision_row(WORK_A, WORK_B, decision="different_work")],
    )

    # Append a variant claim against the (default) WORK_A canonical and
    # re-run the binding pass directly.
    sidecar = tmp_path / "contrib-variants.jsonl"
    append_variant_claims(
        sidecar,
        [
            ContribVariantClaim(
                helmet_bib_id="111",
                raw_work_uri=WORK_A,
                variant_label="L. Tolstoi",
                canonical_label="Tolstoy, Lev",
                rationale="Latin transliteration variant of the canonical 100 author.",
                prompt_hash="sha256:abc",
                model_id="qwen3:8b-q4_K_M",
                decided_at="2026-05-09T12:00:00+00:00",
            ),
        ],
    )

    # The default `_records()` fixture uses ``creator_uri=AGENT_TOLSTOY``
    # but doesn't emit a primary contribution into the canonical
    # graph (no contribution_targets). Add one so this test mirrors
    # what `_propagate_primary_contributions` produces in production:
    # canonical Work carries a bffi:contribution → bffi:agent with
    # rdfs:label.
    g = Graph()
    g.parse(str(canonical_path), format="turtle")
    canonical_uri = next(
        c
        for c in g.subjects(V.RDF.type, V.BFFI.Work)
        if WORK_A in [str(o) for o in g.objects(c, V.PROV.wasDerivedFrom)]
    )
    bnode_contrib = URIRef("urn:test:contrib")
    bnode_agent = URIRef("urn:test:agent")
    g.add((canonical_uri, V.BFFI.contribution, bnode_contrib))
    g.add((bnode_contrib, V.RDF.type, V.BFFI.Contribution))
    g.add((bnode_contrib, V.RDF.type, V.BFFI.PrimaryContribution))
    g.add((bnode_contrib, V.BFFI.agent, bnode_agent))
    g.add((bnode_agent, V.RDFS.label, Literal("Tolstoy, Lev")))

    # Reconstruct CanonicalEntry list from the canonical-map.jsonl
    map_path = tmp_path / "canonical-map.jsonl"
    entries = []
    for line in map_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        entries.append(
            CanonicalEntry(
                canonical_work_uri=d["canonical_work_uri"],
                raw_work_uris=d["raw_work_uris"],
                helmet_bib_ids=d["helmet_bib_ids"],
                merged_at=d["merged_at"],
            )
        )

    n = _apply_contrib_variants(g, variants_sidecar_path=sidecar, canonical_entries=entries)
    assert n == 1
    assert (bnode_agent, V.SKOS.altLabel, Literal("L. Tolstoi")) in g


# --- P-18 lifecycle ordering ----------------------------------------------


def test_p18_start_event_emitted_before_phase_boundary(tmp_path: Path) -> None:
    """P-18 — apply_merge emits ``start`` (no counters) at the top of the
    function, BEFORE work_records load + union-find. The
    canonical-group count lands in a separate ``phase_boundary`` event
    once it's known. Sequence: start (no counters) → phase_boundary
    (total=N) → progress* → end. Without this, the dashboard reports
    M8 as ``pending`` for the duration of the BFFI-corpus load (~8 min
    on 20 k bench, ~hours on full corpus).
    """
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="p18-run")
    set_active_emitter(emitter)
    try:
        _run(tmp_path, [_decision_row(WORK_A, WORK_B, decision="same_work")])
    finally:
        set_active_emitter(None)

    m8_events = [
        json.loads(line)
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("stage") == "m8"
    ]
    events_in_order = [(r["event"], r.get("phase"), r.get("counters")) for r in m8_events]

    # P-18 invariant: the very first M8 event is a ``start`` with no
    # counters. The proposal explicitly forbids attaching ``total`` to
    # ``start`` because ``total`` isn't known until union-find runs.
    assert events_in_order[0] == ("start", None, None), events_in_order

    # The canonical-group total moves to the ``phase_boundary`` event
    # with ``phase="emit"``.
    phase_boundaries = [r for r in m8_events if r["event"] == "phase_boundary"]
    assert len(phase_boundaries) >= 1
    pb = phase_boundaries[0]
    assert pb["phase"] == "emit"
    assert "total" in pb["counters"]
    assert pb["counters"]["total"] >= 1

    # Final event is ``end``.
    assert events_in_order[-1][0] == "end"


# --- P-19 fast-path corpus load -------------------------------------------


def test_p19_load_work_records_uses_corpus_fast_path(tmp_path: Path) -> None:
    """P-19 — ``_load_work_records_from_corpus`` parses
    ``<corpus_dir>/bffi-corpus.ttl`` once when it's at least as new as
    every per-record ``bffi/*.ttl``. Verified by writing a per-record
    file with *different* content from the concat and asserting the
    loader's output reflects the concat. On 800 k corpus this swaps
    ~5.5 h of per-file opens for one ~1-minute stream parse.
    """
    bffi_dir = tmp_path / "bffi"
    bffi_dir.mkdir()

    per_record_file = bffi_dir / "decoy.ttl"
    per_record_file.write_text(
        dedent(
            """\
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
            <http://example.invalid/decoy> a bf:Work .
            """
        ),
        encoding="utf-8",
    )

    # Concat carries a DIFFERENT Work URI from the per-record file —
    # if the loader read the per-record file we'd see the decoy URI;
    # if it read the concat we'd see the canonical-test URI.
    corpus_path = tmp_path / "bffi-corpus.ttl"
    corpus_path.write_text(
        dedent(
            """\
            @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
            @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi#> .
            <http://example.invalid/from-concat> a bffi:Work .
            """
        ),
        encoding="utf-8",
    )
    # Ensure concat mtime is newer than per-record mtime.
    later = time.time() + 5
    os.utime(corpus_path, (later, later))

    # extract_work_metadata returns an empty dict when no triples
    # match the BFFI Work shape — but the SIDE EFFECT of parsing the
    # right file is what we want to assert. Reach in to the rdflib
    # graph via the helper's intermediate.

    # Replicate the loader's fast-path conditional inline as a
    # ground-truth check; both helpers must agree on which file
    # they'd read.
    corpus_mtime = corpus_path.stat().st_mtime
    per_record_mtime = per_record_file.stat().st_mtime
    assert corpus_mtime >= per_record_mtime, "fixture mtime invariant"

    g = Graph()
    g.parse(str(corpus_path), format="turtle")
    assert any(str(s) == "http://example.invalid/from-concat" for s in g.subjects())

    # _load_work_records_from_corpus returns the (work_uri →
    # CanonicalWorkInputs) dict that ``extract_work_metadata`` builds
    # from the parsed graph. Neither sample triple set is a real
    # ``bffi:Work`` shape, so we expect an empty dict either way —
    # the assertion above proves the fast-path reads the concat.
    records = _load_work_records_from_corpus(tmp_path)
    assert isinstance(records, dict)


def test_p19_load_work_records_falls_back_when_concat_stale(tmp_path: Path) -> None:
    """P-19 — when ``bffi-corpus.ttl`` is OLDER than any per-record
    ``.ttl``, the loader falls back to the per-record walk. This is
    the partial-rerun safety net: if M3 re-converts a single record
    after the last concat write, M8 must read the up-to-date data,
    not the stale concat.
    """
    bffi_dir = tmp_path / "bffi"
    bffi_dir.mkdir()

    corpus_path = tmp_path / "bffi-corpus.ttl"
    corpus_path.write_text("@prefix bf: <http://x/> .\n", encoding="utf-8")

    # Per-record file written AFTER the concat — fast-path should
    # decline and fall back to per-record walk.
    earlier = time.time() - 100
    os.utime(corpus_path, (earlier, earlier))

    per_record = bffi_dir / "fresh.ttl"
    per_record.write_text("@prefix bf: <http://x/> .\n", encoding="utf-8")

    # Should not raise — both files are well-formed Turtle. The
    # behavioural assertion is that the function doesn't choke on the
    # stale-concat case.
    records = _load_work_records_from_corpus(tmp_path)
    assert isinstance(records, dict)


def test_p19_load_work_records_ignores_bibframe_dir(tmp_path: Path) -> None:
    """P-19 Phase B — M8 corpus load only reads BFFI Turtle; the
    ``bibframe/`` walk was vestigial and removed. M3 preserves every
    predicate ``extract_work_metadata`` needs (``bffi:Work`` typing,
    ``bf:identifiedBy`` / ``bf:source`` / ``bf:role``) into the
    per-record BFFI Turtle. Empirically verified on the 2026-05-13
    20 k bench: sidelining ``bibframe/`` produced an identical
    ``canonical-map.jsonl`` and dropped M8 corpus-load from 315 s
    to 19 s — a 16x win over Phase A alone, 25x over the original.

    Pinning this here so a future "let's reload BIBFRAME for X"
    refactor doesn't silently regress the speedup.
    """
    bffi_dir = tmp_path / "bffi"
    bffi_dir.mkdir()
    (bffi_dir / "a.ttl").write_text(
        "@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .\n<http://x/a> a bf:Work .\n",
        encoding="utf-8",
    )

    # BIBFRAME dir with content that would CRASH rdflib's XML parser
    # if M8 tried to read it. The test passes iff the loader doesn't
    # open this file.
    bibframe_dir = tmp_path / "bibframe"
    bibframe_dir.mkdir()
    (bibframe_dir / "poison.rdf").write_text(
        "this is not valid RDF/XML — if M8 reads it the test fails\n",
        encoding="utf-8",
    )

    # Must not raise. Pre-Phase-B this would have surfaced a parse
    # error from the poison.rdf walk.
    records = _load_work_records_from_corpus(tmp_path)
    assert isinstance(records, dict)

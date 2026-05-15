"""Unit tests for stages/judge phase 2: extractor + batch driver.

The real LLM is never contacted. The cascade is a deterministic stub
keyed on the input pair so the tests can assert on file contents,
checkpoint state, resume semantics, and progress snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.provenance.writer import ProvenanceWriter
from bffi_pipeline.stages.m6 import (
    CHECKPOINT_INTERVAL,
    DECISIONS_FILENAME,
    CascadeStep,
    JudgeBatchProgress,
    JudgeCheckpoint,
    JudgeOutcome,
    WorkMatchDecision,
    WorkRecord,
    extract_work_records,
    judge_batch,
)
from bffi_pipeline.stages.m6 import runner as judge


@pytest.fixture(autouse=True)
def _redirect_default_cache_path(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-test redirect so judge_batch's default cache path stays inside tmp.

    Without this, ``judge_batch`` called without an explicit ``cache=`` opens
    ``data/judge-cache.sqlite`` next to the working directory — a path that
    does not exist in the test sandbox.
    """
    cache_path = tmp_path_factory.mktemp("judge-cache") / "judge-cache.sqlite"
    monkeypatch.setattr(judge, "default_cache_path", lambda: cache_path)


# --- extract_work_records -------------------------------------------------


def _build_graph() -> Graph:
    """Two-Work BFFI graph mirroring the M3 output shape."""
    g = Graph()
    work_a = URIRef("http://urn.fi/URN:NBN:fi:bib:work:aaa")
    work_b = URIRef("http://urn.fi/URN:NBN:fi:bib:work:bbb")
    expr_a = URIRef("http://urn.fi/URN:NBN:fi:bib:expression:aaa")
    expr_b = URIRef("http://urn.fi/URN:NBN:fi:bib:expression:bbb")
    contrib_a = URIRef("http://example.org/contrib/a")
    contrib_b = URIRef("http://example.org/contrib/b")
    agent = URIRef("http://example.org/agent/tolstoy")
    lang_fi = URIRef("http://id.loc.gov/vocabulary/languages/fin")
    lang_ru = URIRef("http://id.loc.gov/vocabulary/languages/rus")
    content_txt = URIRef("http://id.loc.gov/vocabulary/contentTypes/txt")

    g.add((work_a, RDF.type, V.BFFI.Work))
    g.add((work_a, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work_a, V.BFFI.originDate, Literal("1869")))
    g.add((work_a, V.BFFI.contribution, contrib_a))
    g.add((contrib_a, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib_a, V.BFFI.agent, agent))
    g.add((agent, RDFS.label, Literal("Tolstoy, Leo,")))
    g.add((work_a, V.BFFI.hasExpression, expr_a))
    g.add((expr_a, RDF.type, V.BFFI.Expression))
    g.add((expr_a, V.BFFI.language, lang_fi))
    g.add((expr_a, V.BFFI.content, content_txt))
    g.add((expr_a, V.SKOS.altLabel, Literal("War and Peace")))

    g.add((work_b, RDF.type, V.BFFI.Work))
    g.add((work_b, V.SKOS.prefLabel, Literal("Война и мир", lang="ru")))
    g.add((work_b, V.BFFI.contribution, contrib_b))
    g.add((contrib_b, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib_b, V.BFFI.agent, agent))
    g.add((work_b, V.BFFI.hasExpression, expr_b))
    g.add((expr_b, RDF.type, V.BFFI.Expression))
    g.add((expr_b, V.BFFI.language, lang_ru))
    g.add((expr_b, V.BFFI.content, content_txt))
    return g


def test_extract_pulls_creator_title_language_year_content() -> None:
    records = extract_work_records(_build_graph())
    a = records["http://urn.fi/URN:NBN:fi:bib:work:aaa"]
    assert a.creator == "Tolstoy, Leo,"
    assert a.creator_uri == "http://example.org/agent/tolstoy"
    assert a.preferred_title == "Sota ja rauha"
    assert a.expression_language == "fin"
    assert a.content_type == "txt"
    assert a.date_of_origin == "1869"
    assert "War and Peace" in a.variant_titles

    b = records["http://urn.fi/URN:NBN:fi:bib:work:bbb"]
    assert b.expression_language == "rus"
    assert b.preferred_title == "Война и мир"


def test_extract_handles_missing_creator_label() -> None:
    g = Graph()
    work = URIRef("http://example.org/w/1")
    contrib = URIRef("http://example.org/c/1")
    agent = URIRef("http://example.org/a/1")
    g.add((work, RDF.type, V.BFFI.Work))
    g.add((work, V.BFFI.contribution, contrib))
    g.add((contrib, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib, V.BFFI.agent, agent))
    # no rdfs:label on the agent — creator string remains None, URI carried through.
    records = extract_work_records(g)
    only = records["http://example.org/w/1"]
    assert only.creator is None
    assert only.creator_uri == "http://example.org/a/1"


# --- judge_batch helpers --------------------------------------------------


WORK_A = "http://urn.fi/URN:NBN:fi:bib:work:aaa"
WORK_B = "http://urn.fi/URN:NBN:fi:bib:work:bbb"
WORK_C = "http://urn.fi/URN:NBN:fi:bib:work:ccc"


def _records() -> dict[str, WorkRecord]:
    return {
        WORK_A: WorkRecord(record_id=WORK_A, creator="A", preferred_title="X"),
        WORK_B: WorkRecord(record_id=WORK_B, creator="B", preferred_title="Y"),
        WORK_C: WorkRecord(record_id=WORK_C, creator="C", preferred_title="Z"),
    }


def _candidate(
    work_a: str, work_b: str, *, band: str = "escalate", sim: float = 0.84
) -> dict[str, Any]:
    return {
        "work_a": work_a,
        "work_b": work_b,
        "similarity": sim,
        "block_a": "blk-1",
        "block_b": "blk-2",
        "cross_block": True,
        "band": band,
    }


def _write_candidates(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _make_decision(
    *,
    decision: str = "same_work",
    confidence: float = 0.95,
    matching: list[str] | None = None,
) -> WorkMatchDecision:
    return WorkMatchDecision(
        decision=decision,  # type: ignore[arg-type]
        confidence=confidence,
        rationale="Same author and original_language; B is the Finnish Expression of A.",
        matching_fields=matching if matching is not None else ["creator", "original_language"],
        diverging_fields=["preferred_title"],
    )


def _scripted_cascade(
    decisions: dict[tuple[str, str], JudgeOutcome],
) -> Any:
    """Return a cascade fn replacement keyed on (record_a.record_id, record_b.record_id)."""

    def cascade(
        record_a: WorkRecord,
        record_b: WorkRecord,
        sim: float,
        **kwargs: Any,
    ) -> JudgeOutcome:
        key = (record_a.record_id, record_b.record_id)
        if key not in decisions:
            raise AssertionError(f"Test cascade has no scripted outcome for {key}")
        return decisions[key]

    return cascade


def _outcome(
    decision: WorkMatchDecision,
    *,
    used_cascade: bool = False,
    primary_cache_hit: bool = False,
    fallback_cache_hit: bool = False,
) -> JudgeOutcome:
    steps = [
        CascadeStep(
            stage=judge.STAGE_PRIMARY,
            model_name="primary",
            decision=decision,
            cache_hit=primary_cache_hit,
            latency_seconds=0.01,
        )
    ]
    if used_cascade:
        steps.append(
            CascadeStep(
                stage=judge.STAGE_SECOND_OPINION,
                model_name="fallback",
                decision=decision,
                cache_hit=fallback_cache_hit,
                latency_seconds=0.02,
            )
        )
    return JudgeOutcome(final=decision, steps=steps)


# --- judge_batch: input filtering + output JSONL --------------------------


def test_batch_processes_escalate_via_cascade_skips_reject(tmp_path: Path) -> None:
    """Escalate band feeds the LLM cascade; reject band is dropped
    entirely. Auto-merge band gets its own path — see
    :func:`test_batch_writes_synthetic_same_work_for_auto_merge_band`."""
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(
        candidates,
        [
            _candidate(WORK_A, WORK_B, band="escalate"),
            _candidate(WORK_B, WORK_C, band="reject"),  # filtered out
        ],
    )
    decisions = {(WORK_A, WORK_B): _outcome(_make_decision())}

    result = judge_batch(
        candidates,
        out,
        work_records=_records(),
        cascade=_scripted_cascade(decisions),
    )
    assert result.total_pairs == 1  # escalate-band count
    assert result.auto_merged == 0
    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["work_a"] == WORK_A
    assert rows[0]["work_b"] == WORK_B
    assert rows[0]["decision"] == "same_work"
    assert rows[0]["used_cascade"] is False


def test_batch_writes_synthetic_same_work_for_auto_merge_band(tmp_path: Path) -> None:
    """Spec § 6: similarity ≥ 0.90 auto-merge band merges without an
    LLM call. The batch driver emits one synthetic ``same_work`` row
    per auto-merge candidate, tagged with the embedding stage so
    provenance can distinguish embedding-only from LLM-judged
    decisions. The LLM cascade is NOT invoked for these."""
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(
        candidates,
        [
            _candidate(WORK_A, WORK_B, band="auto-merge", sim=1.0),
            _candidate(WORK_A, WORK_C, band="auto-merge", sim=0.95),
            _candidate(WORK_B, WORK_C, band="reject"),  # still skipped
        ],
    )

    def _exploding_cascade(*args: object, **kwargs: object) -> object:
        msg = "cascade must NOT be invoked for auto-merge band"
        raise AssertionError(msg)

    result = judge_batch(
        candidates,
        out,
        work_records=_records(),
        cascade=_exploding_cascade,
    )
    assert result.total_pairs == 0  # zero escalate-band candidates
    assert result.auto_merged == 2
    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) == 2
    for row in rows:
        assert row["decision"] == "same_work"
        assert row["used_cascade"] is False
        assert row["cascade"][0]["stage"] == "auto-merge-embedding"
        assert row["cascade"][0]["model"] == "BAAI/bge-m3"
        # Confidence equals the embedding similarity for auto-merge rows.
        assert row["confidence"] == row["cascade"][0]["confidence"]


def test_batch_processes_mixed_band_input_into_separate_decision_streams(
    tmp_path: Path,
) -> None:
    """Real M5 output mixes auto-merge + escalate + reject. The
    auto-merge rows write first (deterministic, no LLM); the escalate
    rows then go through the cascade. Reject rows never appear in the
    output."""
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(
        candidates,
        [
            _candidate(WORK_A, WORK_B, band="auto-merge", sim=0.97),
            _candidate(WORK_A, WORK_C, band="escalate"),
            _candidate(WORK_B, WORK_C, band="reject"),
        ],
    )
    decisions = {(WORK_A, WORK_C): _outcome(_make_decision())}
    result = judge_batch(
        candidates,
        out,
        work_records=_records(),
        cascade=_scripted_cascade(decisions),
    )
    assert result.total_pairs == 1  # escalate-band only counted in total_pairs
    assert result.auto_merged == 1
    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    # Auto-merge written first, then escalate.
    assert len(rows) == 2
    assert rows[0]["cascade"][0]["stage"] == "auto-merge-embedding"
    assert rows[1]["cascade"][0]["stage"] != "auto-merge-embedding"


def test_batch_writes_full_decision_payload(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(candidates, [_candidate(WORK_A, WORK_B)])
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision(), used_cascade=True, fallback_cache_hit=True)
    }

    judge_batch(candidates, out, work_records=_records(), cascade=_scripted_cascade(decisions))

    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    row = rows[0]
    assert row["used_cascade"] is True
    assert len(row["cascade"]) == 2
    assert row["cascade"][0]["stage"] == judge.STAGE_PRIMARY
    assert row["cascade"][1]["stage"] == judge.STAGE_SECOND_OPINION
    assert row["cascade"][1]["cache_hit"] is True
    assert "matching_fields" in row
    assert "diverging_fields" in row


def test_batch_invokes_decision_callback_per_pair(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(candidates, [_candidate(WORK_A, WORK_B)])
    decisions = {(WORK_A, WORK_B): _outcome(_make_decision())}

    seen: list[tuple[dict[str, Any], JudgeOutcome]] = []

    def hook(row: dict[str, Any], outcome: JudgeOutcome) -> None:
        seen.append((row, outcome))

    judge_batch(
        candidates,
        out,
        work_records=_records(),
        cascade=_scripted_cascade(decisions),
        decision_callback=hook,
    )
    assert len(seen) == 1
    row, outcome = seen[0]
    assert row["work_a"] == WORK_A
    assert outcome.final.decision == "same_work"


def test_batch_handles_missing_work_record(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(candidates, [_candidate(WORK_A, "http://missing/x")])

    result = judge_batch(
        candidates,
        out,
        work_records={WORK_A: _records()[WORK_A]},
        cascade=_scripted_cascade({}),  # never reached
    )
    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert rows[0]["decision"] == "uncertain"
    assert "missing" in rows[0]["rationale"].lower()
    assert result.decision_counts == {"uncertain": 1}


# --- judge_batch: counters / decision_counts -----------------------------


def test_batch_counts_decisions_by_label(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(
        candidates,
        [
            _candidate(WORK_A, WORK_B),
            _candidate(WORK_A, WORK_C),
            _candidate(WORK_B, WORK_C),
        ],
    )
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision()),
        (WORK_A, WORK_C): _outcome(_make_decision(decision="different_work", confidence=0.95)),
        (WORK_B, WORK_C): _outcome(_make_decision(decision="different_work", confidence=0.85)),
    }
    result = judge_batch(
        candidates, out, work_records=_records(), cascade=_scripted_cascade(decisions)
    )
    assert result.decision_counts == {"same_work": 1, "different_work": 2}


# --- Checkpoint + resume + restart ---------------------------------------


def test_batch_writes_checkpoint_at_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override CHECKPOINT_INTERVAL for the test to keep it fast."""
    monkeypatch.setattr(judge, "CHECKPOINT_INTERVAL", 2)

    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    rows_in = [
        _candidate(WORK_A, WORK_B),
        _candidate(WORK_A, WORK_C),
        _candidate(WORK_B, WORK_C),
    ]
    _write_candidates(candidates, rows_in)
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision()),
        (WORK_A, WORK_C): _outcome(_make_decision()),
        (WORK_B, WORK_C): _outcome(_make_decision()),
    }
    judge_batch(candidates, out, work_records=_records(), cascade=_scripted_cascade(decisions))
    ckpt_path = out.with_name(out.name + ".checkpoint")
    assert ckpt_path.is_file()
    ckpt = JudgeCheckpoint.from_json(ckpt_path.read_text(encoding="utf-8"))
    assert ckpt.last_completed_idx == 2  # zero-indexed; 3 pairs total
    assert ckpt.total_pairs == 3


def test_resume_picks_up_at_last_completed_idx(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(
        candidates,
        [
            _candidate(WORK_A, WORK_B),
            _candidate(WORK_A, WORK_C),
            _candidate(WORK_B, WORK_C),
        ],
    )
    # Pretend the first pair completed; checkpoint records idx=0.
    out.write_text(
        json.dumps(
            {
                "work_a": WORK_A,
                "work_b": WORK_B,
                "similarity": 0.84,
                "block_a": "blk-1",
                "block_b": "blk-2",
                "cross_block": True,
                "decision": "same_work",
                "confidence": 0.95,
                "rationale": "pre-existing decision row from a prior run",
                "matching_fields": ["creator"],
                "diverging_fields": [],
                "used_cascade": False,
                "cascade": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ckpt_path = out.with_name(out.name + ".checkpoint")
    ckpt = JudgeCheckpoint(
        start_time="2026-05-09T00:00:00+00:00",
        last_completed_idx=0,
        total_pairs=3,
        cache_hits=0,
        fresh_calls=1,
        cascade_used=0,
    )
    ckpt_path.write_text(ckpt.to_json(), encoding="utf-8")

    decisions = {
        # First pair scripted should NOT be invoked — already in JSONL.
        (WORK_A, WORK_C): _outcome(_make_decision()),
        (WORK_B, WORK_C): _outcome(_make_decision()),
    }
    result = judge_batch(
        candidates, out, work_records=_records(), cascade=_scripted_cascade(decisions)
    )
    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) == 3
    assert rows[0]["rationale"] == "pre-existing decision row from a prior run"
    assert result.completed == 3


def test_restart_wipes_existing_output_and_checkpoint(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(candidates, [_candidate(WORK_A, WORK_B)])
    ckpt_path = out.with_name(out.name + ".checkpoint")
    out.write_text(
        json.dumps({"stale": "row from a prior run that --restart should erase"}) + "\n",
        encoding="utf-8",
    )
    ckpt_path.write_text("{}", encoding="utf-8")

    decisions = {(WORK_A, WORK_B): _outcome(_make_decision())}
    result = judge_batch(
        candidates,
        out,
        work_records=_records(),
        resume=False,
        cascade=_scripted_cascade(decisions),
    )
    rows = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) == 1
    assert "stale" not in rows[0]
    assert result.completed == 1


# --- Progress reporting ---------------------------------------------------


def test_progress_callback_carries_eta_and_cache_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(judge, "CHECKPOINT_INTERVAL", 1)
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(
        candidates,
        [_candidate(WORK_A, WORK_B), _candidate(WORK_A, WORK_C)],
    )
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision(), primary_cache_hit=True),
        (WORK_A, WORK_C): _outcome(_make_decision()),
    }
    snapshots: list[JudgeBatchProgress] = []
    judge_batch(
        candidates,
        out,
        work_records=_records(),
        cascade=_scripted_cascade(decisions),
        progress_callback=snapshots.append,
    )
    assert len(snapshots) == 2
    final = snapshots[-1]
    assert final.completed == 2
    assert final.total == 2
    assert final.cache_hits == 1
    assert final.fresh_calls == 1


def test_progress_render_format() -> None:
    snap = JudgeBatchProgress(
        completed=12_400,
        total=50_000,
        cache_hits=8_200,
        fresh_calls=4_200,
        cascade_used=400,
        elapsed_seconds=12_400 * 4.2,
        eta_seconds=37_600 * 4.2,
    )
    text = snap.render()
    assert "12,400 / 50,000 pairs" in text
    assert "4.2s/pair" in text
    assert "ETA" in text
    assert "8,200 cache hits" in text
    assert "4,200 fresh calls" in text


# --- Failure-mode plumbing -----------------------------------------------


def test_missing_candidates_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        judge_batch(tmp_path / "missing.jsonl", tmp_path / "out.jsonl", work_records=_records())


def test_constants_match_expected_layout() -> None:
    assert DECISIONS_FILENAME == "judge-decisions.jsonl"
    assert CHECKPOINT_INTERVAL == 100


# --- Provenance integration (M6 phase 2b ↔ M7) ---------------------------


def test_provenance_writer_emits_one_activity_per_cascade_step(tmp_path: Path) -> None:
    """A pair that runs both primary AND fallback should produce TWO Activities."""
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    prov_path = tmp_path / "provenance.ttl"
    _write_candidates(candidates, [_candidate(WORK_A, WORK_B)])
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision(), used_cascade=True),
    }

    with ProvenanceWriter(prov_path) as writer:
        judge_batch(
            candidates,
            out,
            work_records=_records(),
            cascade=_scripted_cascade(decisions),
            provenance_writer=writer,
        )

    g = Graph()
    g.parse(str(prov_path), format="turtle")
    activities = list(g.subjects(V.RDF.type, V.WorkMergeDecision))
    assert len(activities) == 2
    stages = sorted(str(o) for _, _, o in g.triples((None, V.stage, None)))
    assert stages == sorted(["llm-judge-primary", "llm-judge-second-opinion"])


def test_provenance_writer_emits_software_agent_once_per_model(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    prov_path = tmp_path / "provenance.ttl"
    _write_candidates(
        candidates,
        [
            _candidate(WORK_A, WORK_B),
            _candidate(WORK_A, WORK_C),
        ],
    )
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision(), used_cascade=True),
        (WORK_A, WORK_C): _outcome(_make_decision(), used_cascade=True),
    }

    with ProvenanceWriter(prov_path) as writer:
        judge_batch(
            candidates,
            out,
            work_records=_records(),
            cascade=_scripted_cascade(decisions),
            provenance_writer=writer,
        )

    g = Graph()
    g.parse(str(prov_path), format="turtle")
    agents = list(g.subjects(V.RDF.type, V.PROV.SoftwareAgent))
    # Two pairs, each with primary+fallback → 2 distinct model URIs total.
    assert len(agents) == 2


def test_concurrency_preserves_input_order_in_output_jsonl(tmp_path: Path) -> None:
    """Per spec, output rows must match input order even when judging in parallel."""
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    rows_in = [_candidate(WORK_A, WORK_B), _candidate(WORK_A, WORK_C), _candidate(WORK_B, WORK_C)]
    _write_candidates(candidates, rows_in)

    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision(matching=["a-b"])),
        (WORK_A, WORK_C): _outcome(_make_decision(matching=["a-c"])),
        (WORK_B, WORK_C): _outcome(_make_decision(matching=["b-c"])),
    }
    judge_batch(
        candidates,
        out,
        work_records=_records(),
        cascade=_scripted_cascade(decisions),
        concurrency=4,
    )
    rows_out = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert [(r["work_a"], r["work_b"]) for r in rows_out] == [
        (WORK_A, WORK_B),
        (WORK_A, WORK_C),
        (WORK_B, WORK_C),
    ]
    assert {r["matching_fields"][0] for r in rows_out} == {"a-b", "a-c", "b-c"}


def test_concurrency_zero_or_negative_raises(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    _write_candidates(candidates, [_candidate(WORK_A, WORK_B)])
    with pytest.raises(ValueError, match="concurrency"):
        judge_batch(
            candidates,
            out,
            work_records=_records(),
            cascade=_scripted_cascade({(WORK_A, WORK_B): _outcome(_make_decision())}),
            concurrency=0,
        )


def test_provenance_writer_records_cache_hit_flag(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    out = tmp_path / "decisions.jsonl"
    prov_path = tmp_path / "provenance.ttl"
    _write_candidates(candidates, [_candidate(WORK_A, WORK_B)])
    decisions = {
        (WORK_A, WORK_B): _outcome(_make_decision(), primary_cache_hit=True),
    }

    with ProvenanceWriter(prov_path) as writer:
        judge_batch(
            candidates,
            out,
            work_records=_records(),
            cascade=_scripted_cascade(decisions),
            provenance_writer=writer,
        )

    g = Graph()
    g.parse(str(prov_path), format="turtle")
    cache_hit_values = {str(o) for _, _, o in g.triples((None, V.cacheHit, None))}
    assert "true" in cache_hit_values
    # Exactly one cacheHit triple (only the primary step ran for this pair).
    triples = list(g.triples((None, V.cacheHit, None)))
    assert len(triples) == 1
    assert triples[0][2] == Literal(True)

"""Unit tests for eval/grow: gold-set growth from human-overrides (M12 phase 3).

Fuseki is exercised through ``httpx.MockTransport`` — no live SPARQL.
The query body itself is loaded and asserted on so a regression that
breaks the SELECT shape surfaces here, not in the next manual run.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from bffi_pipeline.eval.grow import (
    DEFAULT_GROW_QUERY_PATH,
    GoldCandidate,
    fetch_override_candidates,
    grow,
    grow_query_text,
    write_candidates,
)

# --- The committed query ---------------------------------------------------


def test_grow_query_file_exists_and_carries_required_clauses() -> None:
    """The SPARQL file must be on disk and reference the override pattern."""
    text = grow_query_text()
    assert "bffi-prov:WorkMergeDecision" in text
    assert "bffi-prov:HumanReview" in text
    assert "prov:wasInformedBy" in text
    assert '"overridden"' in text
    # The two-source-Works pattern is the bit that's easy to break:
    assert "?workA" in text
    assert "?workB" in text


def test_grow_query_text_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        grow_query_text(tmp_path / "no-such.rq")


def test_default_query_path_is_in_repo_sparql_dir() -> None:
    """Path must remain under sparql/queries/ so cataloguers can find it."""
    assert DEFAULT_GROW_QUERY_PATH.parent.name == "queries"
    assert DEFAULT_GROW_QUERY_PATH.parent.parent.name == "sparql"


# --- Mock-Fuseki helpers ---------------------------------------------------


def _binding(value: str | None, datatype: str = "literal") -> dict[str, object]:
    """Build a SPARQL JSON binding cell."""
    if value is None:
        return {}
    return {"type": datatype, "value": value}


def _row(**kwargs: str | None) -> dict[str, dict[str, object]]:
    """Compact builder for a SELECT binding row."""
    return {k: _binding(v) for k, v in kwargs.items() if v is not None}


def _mock_fuseki_transport(rows: list[dict[str, dict[str, object]]]) -> httpx.MockTransport:
    """Return a transport that answers any ``POST /sparql`` with ``rows``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/sparql")
        return httpx.Response(
            200,
            json={"head": {"vars": []}, "results": {"bindings": rows}},
        )

    return httpx.MockTransport(handler)


# --- fetch_override_candidates --------------------------------------------


def test_fetch_returns_one_candidate_per_binding() -> None:
    rows = [
        _row(
            activity="urn:bib:review/01HX2A0001",
            decision="same_work",
            confidence="0.78",
            rationale="LLM rationale text long enough.",
            reviewDecision="overridden",
            reviewNote="Cataloguer disagreed: different translations.",
            workA="http://urn.fi/URN:NBN:fi:bib:work:aaa",
            creatorA="Pushkin, Aleksandr,",
            titleA="Dubrovskij",
            languageA="rus",
            bibIdA="111",
            workB="http://urn.fi/URN:NBN:fi:bib:work:bbb",
            creatorB="Pushkin, Aleksandr,",
            titleB="Aatelisrosvo Dubrovskij",
            languageB="fin",
            bibIdB="222",
        ),
    ]
    client = httpx.Client(transport=_mock_fuseki_transport(rows))
    candidates = fetch_override_candidates(
        fuseki_url="http://fuseki:3030/bffi",
        http_client=client,
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert isinstance(cand, GoldCandidate)
    assert cand.id == "grow-01HX2A0001"
    # LLM said same_work; cataloguer overrode → expected flips to different_work.
    assert cand.expected == "different_work"
    assert cand.category is None  # cataloguer fills in
    assert cand.holdout is False
    assert cand.added_by == "grow-from-overrides"
    assert cand.record_a["creator"] == "Pushkin, Aleksandr,"
    assert cand.record_a["helmet_bib_id"] == "111"
    assert cand.record_b["language"] == "fin"
    assert "Cataloguer disagreed" in (cand.notes or "")
    assert "original LLM decision: same_work" in (cand.notes or "")


def test_fetch_inverts_different_work_decision_to_same_work() -> None:
    """When the LLM said different_work, the override means same_work."""
    rows = [
        _row(
            activity="urn:bib:review/01HX2A0002",
            decision="different_work",
            confidence="0.81",
            rationale="LLM thought different.",
            reviewDecision="overridden",
            reviewNote="Translation pair, same Work.",
            workA="urn:bib:work:aa",
            workB="urn:bib:work:bb",
        ),
    ]
    client = httpx.Client(transport=_mock_fuseki_transport(rows))
    candidates = fetch_override_candidates(
        fuseki_url="http://fuseki:3030/bffi",
        http_client=client,
    )
    assert candidates[0].expected == "same_work"


def test_fetch_handles_uncertain_original_with_default_inverse() -> None:
    """``decision="uncertain"`` falls through to ``same_work`` as a useful default."""
    rows = [
        _row(
            activity="urn:bib:review/01HX2A0003",
            decision="uncertain",
            confidence="0.5",
            rationale="LLM was uncertain.",
            reviewDecision="overridden",
            reviewNote="Cataloguer: actually same.",
            workA="urn:bib:work:aa",
            workB="urn:bib:work:bb",
        ),
    ]
    client = httpx.Client(transport=_mock_fuseki_transport(rows))
    candidates = fetch_override_candidates(
        fuseki_url="http://fuseki:3030/bffi",
        http_client=client,
    )
    assert candidates[0].expected == "same_work"


def test_fetch_tolerates_missing_bffi_works_fields() -> None:
    """OPTIONALs in the SPARQL → some bindings are absent. Don't crash."""
    rows = [
        _row(
            activity="urn:bib:review/01HX2A0004",
            decision="same_work",
            confidence="0.78",
            rationale="LLM rationale text long enough.",
            reviewDecision="overridden",
            reviewNote="Cataloguer note.",
            workA="urn:bib:work:aa",
            workB="urn:bib:work:bb",
            # No creator/title/language/bibId on either side.
        ),
    ]
    client = httpx.Client(transport=_mock_fuseki_transport(rows))
    candidates = fetch_override_candidates(
        fuseki_url="http://fuseki:3030/bffi",
        http_client=client,
    )
    cand = candidates[0]
    assert cand.record_a == {}
    assert cand.record_b == {}


def test_fetch_returns_empty_list_when_fuseki_has_no_overrides() -> None:
    client = httpx.Client(transport=_mock_fuseki_transport([]))
    candidates = fetch_override_candidates(
        fuseki_url="http://fuseki:3030/bffi",
        http_client=client,
    )
    assert candidates == []


# --- write_candidates ------------------------------------------------------


def test_write_candidates_emits_one_jsonl_row_per_candidate(tmp_path: Path) -> None:
    candidates = [
        GoldCandidate(
            id="grow-01HX2A0002",
            expected="same_work",
            notes="hello",
            record_a={"creator": "A"},
            record_b={"creator": "B"},
        ),
        GoldCandidate(
            id="grow-01HX2A0001",
            expected="different_work",
            notes="world",
            record_a={"creator": "C"},
            record_b={"creator": "D"},
        ),
    ]
    out = tmp_path / "candidates.jsonl"
    write_candidates(out, candidates)
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    # Sorted by id → 0001 first.
    assert rows[0]["id"] == "grow-01HX2A0001"
    assert rows[1]["id"] == "grow-01HX2A0002"
    # Round-trip carries every dataclass field.
    assert rows[0] == asdict(candidates[1])


def test_write_candidates_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "more" / "candidates.jsonl"
    write_candidates(out, [])
    assert out.is_file()
    assert out.read_text() == ""


def test_write_candidates_atomic_via_tmp_then_rename(tmp_path: Path) -> None:
    """No ``.tmp`` should remain after a successful write."""
    out = tmp_path / "candidates.jsonl"
    write_candidates(
        out,
        [GoldCandidate(id="grow-1", expected="same_work")],
    )
    assert out.is_file()
    assert not (tmp_path / "candidates.jsonl.tmp").exists()


# --- grow end-to-end -------------------------------------------------------


def test_grow_writes_candidates_and_returns_summary(tmp_path: Path) -> None:
    rows = [
        _row(
            activity="urn:bib:review/01HX2A0010",
            decision="same_work",
            confidence="0.78",
            rationale="LLM rationale.",
            reviewDecision="overridden",
            reviewNote="Cataloguer note.",
            workA="urn:bib:work:aa",
            workB="urn:bib:work:bb",
        ),
    ]
    client = httpx.Client(transport=_mock_fuseki_transport(rows))
    out = tmp_path / "grow-candidates.jsonl"
    result = grow(
        fuseki_url="http://fuseki:3030/bffi",
        http_client=client,
        output_path=out,
    )
    assert result.candidates_written == 1
    assert result.output_path == str(out)
    assert out.is_file()
    payload = json.loads(out.read_text().splitlines()[0])
    assert payload["id"] == "grow-01HX2A0010"

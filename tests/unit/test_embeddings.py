"""Unit tests for stages/embeddings (M5 Stage 2 candidate generation).

The real embedding model (BAAI/bge-m3) is never loaded in these tests.
Instead, the FAISS round-trip is exercised against a deterministic
fake encoder that maps the known fixture strings to known vectors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.blocking import compute_blocking_key
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.m5 import (
    BAND_AUTO_MERGE,
    BAND_REJECT,
    CANDIDATES_FILENAME,
    IDMAP_FILENAME,
    INDEX_FILENAME,
    Band,
    CandidatePair,
    WorkEmbeddingInput,
    build_index,
    classify_band,
    embedding_input_string,
    extract_embedding_inputs,
    query_candidates,
    to_blocking_key,
)
from bffi_pipeline.stages.m5 import build as _m5_build
from bffi_pipeline.stages.m5 import runner as embeddings
from bffi_pipeline.stages.m5.runner import _normalise_year, _short_segment

# --- embedding_input_string -----------------------------------------------


def _make_input(**overrides: str | None) -> WorkEmbeddingInput:
    base: dict[str, str | None] = {
        "work_uri": "http://urn.fi/URN:NBN:fi:bib:work:abc",
        "creator": "Tolstoy, Leo,",
        "title": "Sota ja rauha",
        "language": "fin",
        "year": "1869",
        "content_type": "txt",
    }
    base.update(overrides)
    return WorkEmbeddingInput(**base)  # type: ignore[arg-type]


def test_input_string_has_fixed_field_order() -> None:
    s = embedding_input_string(_make_input())
    assert s == (
        "creator: Tolstoy, Leo, | title: Sota ja rauha | language: fin | year: 1869 | type: txt"
    )


def test_input_string_keeps_empty_fields_so_vectors_remain_stable() -> None:
    s = embedding_input_string(_make_input(year=None, content_type=None))
    # Five pipe-separated chunks regardless of which fields are populated.
    assert s.count(" | ") == 4
    assert "year:" in s and "type:" in s


def test_input_string_is_idempotent() -> None:
    work = _make_input()
    assert embedding_input_string(work) == embedding_input_string(work)


# --- _normalise_year ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1869", "1869"),
        ("[2026]", "2026"),
        ("c2020", "2020"),
        ("©2026", "2026"),
        ("1869-01-01", "1869"),
        ("2026-03-05/2026-03-12", "2026"),
        (None, None),
        ("no digits here", None),
        ("not a year: 12 or 999", None),  # 4-digit-only requirement
    ],
)
def test_normalise_year(raw: str | None, expected: str | None) -> None:
    assert _normalise_year(raw) == expected


# --- _short_segment -------------------------------------------------------


def test_short_segment_strips_known_prefix() -> None:
    assert (
        _short_segment(
            "http://id.loc.gov/vocabulary/contentTypes/txt",
            "http://id.loc.gov/vocabulary/contentTypes/",
        )
        == "txt"
    )


def test_short_segment_passthroughs_unrelated_uri() -> None:
    assert (
        _short_segment(
            "http://example.org/vocab/foo/bar",
            "http://id.loc.gov/vocabulary/contentTypes/",
        )
        == "bar"
    )


def test_short_segment_returns_short_code_unchanged() -> None:
    assert _short_segment("txt", "http://id.loc.gov/vocabulary/contentTypes/") == "txt"


def test_short_segment_returns_none_on_empty_input() -> None:
    assert _short_segment(None, "http://example.org/") is None
    assert _short_segment("", "http://example.org/") is None


# --- classify_band --------------------------------------------------------


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [
        (0.95, "auto-merge"),
        (BAND_AUTO_MERGE, "auto-merge"),
        (0.85, "escalate"),
        (BAND_REJECT + 0.001, "escalate"),
        (BAND_REJECT, "reject"),
        (0.10, "reject"),
    ],
)
def test_classify_band_boundaries(similarity: float, expected: Band) -> None:
    assert classify_band(similarity) == expected


# --- to_blocking_key (parity with M4) -------------------------------------


def test_to_blocking_key_matches_m4_compute_blocking_key() -> None:
    work = _make_input()
    bffi_pipeline_key = to_blocking_key(work)
    workkey_module_key = compute_blocking_key(
        {
            "creator": work.creator,
            "title": work.title,
            "content_type": work.content_type,
        }
    )
    assert bffi_pipeline_key == workkey_module_key


# --- extract_embedding_inputs --------------------------------------------


def _build_synthetic_graph() -> Graph:
    """Two-Work BFFI graph: same author, different works/languages."""
    g = Graph()
    work_a = URIRef("http://urn.fi/URN:NBN:fi:bib:work:aaa")
    work_b = URIRef("http://urn.fi/URN:NBN:fi:bib:work:bbb")
    expr_a = URIRef("http://urn.fi/URN:NBN:fi:bib:expression:aaa")
    expr_b = URIRef("http://urn.fi/URN:NBN:fi:bib:expression:bbb")
    agent_a = URIRef("http://example.org/agent/tolstoy")
    contrib_a = URIRef("http://urn.fi/URN:NBN:fi:bib:contribution:aaa")
    contrib_b = URIRef("http://urn.fi/URN:NBN:fi:bib:contribution:bbb")
    lang_fi = URIRef("http://id.loc.gov/vocabulary/languages/fin")
    lang_en = URIRef("http://id.loc.gov/vocabulary/languages/eng")
    content_txt = URIRef("http://id.loc.gov/vocabulary/contentTypes/txt")

    # Work A — Sota ja rauha (Finnish), originDate 1869
    g.add((work_a, RDF.type, V.BFFI.Work))
    g.add((work_a, V.SKOS.prefLabel, Literal("Sota ja rauha", lang="fi")))
    g.add((work_a, V.BFFI.originDate, Literal("1869")))
    g.add((work_a, V.BFFI.contribution, contrib_a))
    g.add((contrib_a, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib_a, V.BFFI.agent, agent_a))
    g.add((agent_a, RDFS.label, Literal("Tolstoy, Leo,")))
    g.add((work_a, V.BFFI.hasExpression, expr_a))
    g.add((expr_a, RDF.type, V.BFFI.Expression))
    g.add((expr_a, V.BFFI.language, lang_fi))
    g.add((expr_a, V.BFFI.content, content_txt))

    # Work B — War and Peace (English), same author, also originDate 1869
    g.add((work_b, RDF.type, V.BFFI.Work))
    g.add((work_b, V.SKOS.prefLabel, Literal("War and Peace", lang="en")))
    g.add((work_b, V.BFFI.originDate, Literal("c1869")))
    g.add((work_b, V.BFFI.contribution, contrib_b))
    g.add((contrib_b, RDF.type, V.BFFI.PrimaryContribution))
    g.add((contrib_b, V.BFFI.agent, agent_a))
    g.add((work_b, V.BFFI.hasExpression, expr_b))
    g.add((expr_b, RDF.type, V.BFFI.Expression))
    g.add((expr_b, V.BFFI.language, lang_en))
    g.add((expr_b, V.BFFI.content, content_txt))
    return g


def test_extract_pulls_creator_title_language_year_type() -> None:
    g = _build_synthetic_graph()
    by_uri = {w.work_uri: w for w in extract_embedding_inputs(g)}
    a = by_uri["http://urn.fi/URN:NBN:fi:bib:work:aaa"]
    assert a.creator == "Tolstoy, Leo,"
    assert a.title == "Sota ja rauha"
    assert a.language == "fin"
    assert a.year == "1869"
    assert a.content_type == "txt"

    b = by_uri["http://urn.fi/URN:NBN:fi:bib:work:bbb"]
    assert b.language == "eng"
    assert b.year == "1869"  # year extracted from "c1869"


def test_extract_returns_none_for_missing_fields() -> None:
    g = Graph()
    w = URIRef("http://example.org/w/1")
    g.add((w, RDF.type, V.BFFI.Work))
    inputs = list(extract_embedding_inputs(g))
    assert len(inputs) == 1
    only = inputs[0]
    assert only.creator is None
    assert only.title is None
    assert only.language is None
    assert only.year is None
    assert only.content_type is None


# --- Full build → persist → query loop with a fake encoder ----------------


def _fake_embed_factory(
    string_to_vector: dict[str, list[float]],
) -> Any:
    """Return a `_embed` replacement that maps known strings to fixed vectors.

    Vectors do not need to be unit-norm; the fake L2-normalises before
    returning, matching what production `_embed` does. Bypassing
    `_embed` entirely means the test never has to download model
    weights. Tests still require numpy + faiss installed because they
    are project dependencies.
    """

    def fake_embed(
        strings: list[str],
        *,
        model_name: str,
        device: str,
        batch_size: int,
    ) -> Any:
        rows = []
        for s in strings:
            if s not in string_to_vector:
                raise AssertionError(f"Test fake encoder received unexpected input string: {s!r}")
            rows.append(string_to_vector[s])
        matrix = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (matrix / norms).astype(np.float32)

    return fake_embed


def _write_minimal_corpus(corpus_dir: Path) -> tuple[str, str]:
    """Write the synthetic two-Work graph as a single Turtle file.

    Returns the two work URIs.
    """
    g = _build_synthetic_graph()
    bffi_dir = corpus_dir / "bffi"
    bffi_dir.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(bffi_dir / "synthetic.ttl"), format="turtle")
    return (
        "http://urn.fi/URN:NBN:fi:bib:work:aaa",
        "http://urn.fi/URN:NBN:fi:bib:work:bbb",
    )


@pytest.fixture
def fake_encoder(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[float]]:
    """Patch the M5 ``_embed`` shim with a deterministic string-to-vector lookup.

    Patches in two places: ``m5.runner._embed`` (the re-export surface
    older tests / external callers may reach for) and
    ``m5.build._embed`` (the call site ``build_index`` actually goes
    through after P-38 Phase D's module split). Patching only the
    re-export leaves ``build_index`` calling the real encoder.
    """
    table: dict[str, list[float]] = {}
    fake = _fake_embed_factory(table)
    monkeypatch.setattr(embeddings, "_embed", fake)
    monkeypatch.setattr(_m5_build, "_embed", fake)
    return table


def test_build_index_writes_files_and_idmap(
    tmp_path: Path,
    fake_encoder: dict[str, list[float]],
) -> None:
    corpus = tmp_path / "corpus"
    out = tmp_path / "out"
    work_a, work_b = _write_minimal_corpus(corpus)

    inputs = list(extract_embedding_inputs(_build_synthetic_graph()))
    a_in = next(w for w in inputs if w.work_uri == work_a)
    b_in = next(w for w in inputs if w.work_uri == work_b)
    fake_encoder[embedding_input_string(a_in)] = [1.0, 0.0, 0.0]
    fake_encoder[embedding_input_string(b_in)] = [0.95, 0.31, 0.0]  # cos ~ 0.95

    result = build_index(corpus, output_dir=out, model_name="test-fake", device="cpu")
    assert result.n_works == 2
    assert result.ndim == 3
    assert result.model_name == "test-fake"
    assert (out / INDEX_FILENAME).is_file()
    assert (out / IDMAP_FILENAME).is_file()

    idmap = json.loads((out / IDMAP_FILENAME).read_text(encoding="utf-8"))
    rows_by_uri = {row["work_uri"]: row for row in idmap["ids"]}
    assert set(rows_by_uri) == {work_a, work_b}
    # Both Works share author "Tolstoy" → same surname → same blocking key
    # (different titles still pin them to different blocks though).
    assert rows_by_uri[work_a]["blocking_key"].startswith("tolstoy|")
    assert rows_by_uri[work_b]["blocking_key"].startswith("tolstoy|")


def test_build_index_is_idempotent_when_files_are_fresh(
    tmp_path: Path,
    fake_encoder: dict[str, list[float]],
) -> None:
    corpus = tmp_path / "corpus"
    out = tmp_path / "out"
    _write_minimal_corpus(corpus)
    inputs = list(extract_embedding_inputs(_build_synthetic_graph()))
    fake_encoder[embedding_input_string(inputs[0])] = [1.0, 0.0]
    fake_encoder[embedding_input_string(inputs[1])] = [0.9, 0.435]

    first = build_index(corpus, output_dir=out, model_name="test-fake", device="cpu")
    assert first.build_seconds > 0  # actually built
    second = build_index(corpus, output_dir=out, model_name="test-fake", device="cpu")
    assert second.build_seconds == 0.0  # short-circuited


def test_build_index_rebuilds_when_model_name_changes(
    tmp_path: Path,
    fake_encoder: dict[str, list[float]],
) -> None:
    corpus = tmp_path / "corpus"
    out = tmp_path / "out"
    _write_minimal_corpus(corpus)
    inputs = list(extract_embedding_inputs(_build_synthetic_graph()))
    fake_encoder[embedding_input_string(inputs[0])] = [1.0, 0.0]
    fake_encoder[embedding_input_string(inputs[1])] = [0.9, 0.435]

    first = build_index(corpus, output_dir=out, model_name="test-fake-A", device="cpu")
    second = build_index(corpus, output_dir=out, model_name="test-fake-B", device="cpu")
    assert first.build_seconds > 0
    assert second.build_seconds > 0  # different model triggered a rebuild


def test_query_candidates_emits_jsonl_and_band_distribution(
    tmp_path: Path,
    fake_encoder: dict[str, list[float]],
) -> None:
    corpus = tmp_path / "corpus"
    out = tmp_path / "out"
    work_a, work_b = _write_minimal_corpus(corpus)
    inputs = list(extract_embedding_inputs(_build_synthetic_graph()))
    a_in = next(w for w in inputs if w.work_uri == work_a)
    b_in = next(w for w in inputs if w.work_uri == work_b)
    # Vectors picked so the cosine similarity sits in the auto-merge band.
    fake_encoder[embedding_input_string(a_in)] = [1.0, 0.0]
    fake_encoder[embedding_input_string(b_in)] = [0.99, 0.141]  # cos ~ 0.99

    build_index(corpus, output_dir=out, model_name="test-fake", device="cpu")
    stats = query_candidates(out, top_k=5, cross_block=True)

    assert stats.total_pairs == 1
    assert stats.band_counts.get("auto-merge", 0) == 1
    # Same author but different local-language titles ("Sota" vs "War")
    # produce different Stage-1 blocking keys; this is the canonical
    # case the embedding step is meant to bridge.
    assert stats.cross_block_count == 1

    candidates_path = out / CANDIDATES_FILENAME
    assert candidates_path.is_file()
    rows = [json.loads(line) for line in candidates_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    pair = rows[0]
    assert {pair["work_a"], pair["work_b"]} == {work_a, work_b}
    assert pair["band"] == "auto-merge"
    assert pair["cross_block"] is True
    assert pair["block_a"] != pair["block_b"]
    assert pair["similarity"] >= BAND_AUTO_MERGE


def test_query_candidates_drops_cross_block_pairs_by_default(
    tmp_path: Path,
    fake_encoder: dict[str, list[float]],
) -> None:
    """When two Works land in different Stage-1 blocks, the default filter excludes them."""
    g = Graph()
    w1 = URIRef("http://example.org/w/1")
    w2 = URIRef("http://example.org/w/2")
    a1 = URIRef("http://example.org/a/1")
    a2 = URIRef("http://example.org/a/2")
    c1 = URIRef("http://example.org/c/1")
    c2 = URIRef("http://example.org/c/2")
    for w, ag, c, label, title in (
        (w1, a1, c1, "Author One,", "Foo"),
        (w2, a2, c2, "Author Two,", "Bar"),
    ):
        g.add((w, RDF.type, V.BFFI.Work))
        g.add((w, V.SKOS.prefLabel, Literal(title)))
        g.add((w, V.BFFI.contribution, c))
        g.add((c, RDF.type, V.BFFI.PrimaryContribution))
        g.add((c, V.BFFI.agent, ag))
        g.add((ag, RDFS.label, Literal(label)))

    corpus = tmp_path / "corpus"
    bffi_dir = corpus / "bffi"
    bffi_dir.mkdir(parents=True)
    g.serialize(destination=str(bffi_dir / "synthetic.ttl"), format="turtle")

    out = tmp_path / "out"
    inputs = list(extract_embedding_inputs(g))
    s1 = embedding_input_string(inputs[0])
    s2 = embedding_input_string(inputs[1])
    fake_encoder[s1] = [1.0, 0.0]
    fake_encoder[s2] = [0.99, 0.141]  # cos ~ 0.99 — would be auto-merge but blocks differ

    build_index(corpus, output_dir=out, model_name="test-fake", device="cpu")
    stats_default = query_candidates(out, top_k=5, cross_block=False)
    assert stats_default.total_pairs == 0

    stats_cross = query_candidates(out, top_k=5, cross_block=True)
    assert stats_cross.total_pairs == 1
    assert stats_cross.cross_block_count == 1


def test_candidate_pair_dataclass_round_trips_json() -> None:
    pair = CandidatePair(
        work_a="http://example.org/a",
        work_b="http://example.org/b",
        similarity=0.91,
        block_a="tolstoy|sota|txt",
        block_b="tolstoy|sota|txt",
        cross_block=False,
        band="auto-merge",
    )
    encoded = json.dumps(pair.__dict__)
    decoded = json.loads(encoded)
    assert decoded["band"] == "auto-merge"
    assert decoded["similarity"] == pytest.approx(0.91)


def test_query_candidates_raises_when_index_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        query_candidates(tmp_path)

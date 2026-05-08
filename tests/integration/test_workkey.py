"""End-to-end test for stage M4 against the synthetic corpus.

Runs M2 + M3, then loads the combined BFFI + BIBFRAME graph and verifies
that ``compute_blocks`` produces the right per-record keys and that the
extracted creator / title / content-type per Work matches expectations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bffi_pipeline.stages.bf_to_bffi import run as run_m3
from bffi_pipeline.stages.marc_to_bf import run as run_m2
from bffi_pipeline.stages.workkey import (
    compute_blocking_key,
    compute_blocks,
    extract_blocking_inputs,
    load_corpus,
)
from bffi_pipeline.uris import mint_raw_work_uri

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "sample-marcxml"

_EXPECTED_KEYS = {
    "10000001": "tolstoy|sota|txt",
    "10000002": "linna|tuntematon|txt",
    "10000003": "lindgren|pippi|txt",
    "10000004": "sibelius|finlandia|ntm",
    "10000005": "oksanen|puhdistus|txt",
    "10000006": "helsingin|tieteessa|txt",
}


@pytest.fixture(scope="module")
def corpus_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("m4-out")
    run_m2(FIXTURES, output_dir=out)
    run_m3(output_dir=out)
    return out


def test_extracts_creator_title_and_content_for_each_work(corpus_path: Path) -> None:
    g = load_corpus(corpus_path)
    by_work = {entry.work_uri: entry for entry in extract_blocking_inputs(g)}
    for bib_id, expected_key in _EXPECTED_KEYS.items():
        work_uri = mint_raw_work_uri(f"http://urn.fi/URN:NBN:fi:bib:raw/{bib_id}#Work")
        entry = by_work[work_uri]
        assert entry.creator, f"missing creator for {bib_id}"
        assert entry.title, f"missing title for {bib_id}"
        assert entry.content_type, f"missing content type for {bib_id}"
        actual_key = compute_blocking_key(
            {
                "creator": entry.creator,
                "title": entry.title,
                "content_type": entry.content_type,
            }
        )
        assert actual_key == expected_key, f"{bib_id}: got {actual_key}"


def test_compute_blocks_groups_synthetic_corpus_into_singletons(corpus_path: Path) -> None:
    """Each synthetic record is a distinct Work; expect six singleton blocks."""
    stats = compute_blocks(load_corpus(corpus_path))
    assert stats.total_works == len(_EXPECTED_KEYS)
    assert stats.block_count == len(_EXPECTED_KEYS)
    assert stats.size_distribution == {1: len(_EXPECTED_KEYS)}


def test_load_corpus_accepts_single_ttl_file(corpus_path: Path) -> None:
    """Standalone .ttl path: M4 still loads (creator info will be missing)."""
    one_ttl = corpus_path / "bffi" / "10000001.ttl"
    g = load_corpus(one_ttl)
    entries = list(extract_blocking_inputs(g))
    assert len(entries) == 1
    # No bibframe loaded -> no agent label, so creator falls back to anon.
    assert entries[0].title == "Sota ja rauha"
    assert entries[0].creator is None

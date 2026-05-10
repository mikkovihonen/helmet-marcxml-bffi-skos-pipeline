"""Unit tests for ``bffi_pipeline.contrib_variants`` (F2 sidecar)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bffi_pipeline.contrib_variants import (
    DEFAULT_SIDECAR_NAME,
    ContribVariantClaim,
    append_variant_claims,
    load_variant_claims,
    truncate_sidecar,
)


def _make_claim(**overrides: object) -> ContribVariantClaim:
    base: dict[str, object] = {
        "helmet_bib_id": "1714651",
        "raw_work_uri": "http://urn.fi/URN:NBN:fi:bib:work:abc123",
        "variant_label": "Anssi Karttunen",
        "canonical_label": "Karttunen, Assi",
        "rationale": "Latin-script variant of an existing 700 entry; cataloguer typo.",
        "prompt_hash": "sha256:deadbeef",
        "model_id": "qwen3:8b-q4_K_M",
        "decided_at": "2026-05-10T14:00:00+00:00",
    }
    base.update(overrides)
    return ContribVariantClaim.model_validate(base)


# --- Schema -------------------------------------------------------------


def test_claim_minimal_shape() -> None:
    claim = _make_claim()
    assert claim.relator_code_hint is None
    assert claim.role_text_hint is None


def test_claim_with_relator_hint() -> None:
    claim = _make_claim(relator_code_hint="prf", role_text_hint="cembalo")
    assert claim.relator_code_hint == "prf"


def test_claim_extra_fields_rejected() -> None:
    """Schema is closed — typos in writers fail loudly on load."""
    with pytest.raises(ValidationError):
        ContribVariantClaim.model_validate(
            {
                "helmet_bib_id": "1714651",
                "raw_work_uri": "http://x",
                "variant_label": "x",
                "canonical_label": "y",
                "rationale": "...",
                "prompt_hash": "sha256:x",
                "model_id": "x",
                "decided_at": "2026-05-10",
                "secret_extra": True,
            }
        )


def test_claim_rejects_empty_variant_or_canonical() -> None:
    with pytest.raises(ValidationError):
        _make_claim(variant_label="")
    with pytest.raises(ValidationError):
        _make_claim(canonical_label="")


# --- I/O -----------------------------------------------------------------


def test_default_sidecar_name_is_jsonl() -> None:
    assert DEFAULT_SIDECAR_NAME.endswith(".jsonl")


def test_append_creates_file_with_one_row(tmp_path: Path) -> None:
    p = tmp_path / "v.jsonl"
    n = append_variant_claims(p, [_make_claim()])
    assert n == 1
    content = p.read_text(encoding="utf-8")
    assert content.count("\n") == 1  # one row, terminated
    parsed = json.loads(content.strip())
    assert parsed["variant_label"] == "Anssi Karttunen"


def test_append_extends_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "v.jsonl"
    append_variant_claims(p, [_make_claim()])
    append_variant_claims(p, [_make_claim(variant_label="Some Other")])
    rows = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 2


def test_append_empty_iterable_is_noop(tmp_path: Path) -> None:
    p = tmp_path / "v.jsonl"
    n = append_variant_claims(p, [])
    assert n == 0
    assert not p.exists()


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """Sidecar absence is normal (no cascade ran). Loader must not raise."""
    assert load_variant_claims(tmp_path / "missing.jsonl") == []


def test_load_dedupes_on_raw_work_variant_canonical_triple(tmp_path: Path) -> None:
    """If the cascade ran twice and appended overlapping rows, the
    loader collapses them so M8 binds each variant once. Dedup key
    is ``(raw_work_uri, variant_label, canonical_label)``."""
    p = tmp_path / "v.jsonl"
    duplicate = _make_claim()
    append_variant_claims(p, [duplicate, duplicate])
    claims = load_variant_claims(p)
    assert len(claims) == 1


def test_load_keeps_distinct_variants_for_same_record(tmp_path: Path) -> None:
    """Two different variants on one record (e.g. Anssi/Assi *and*
    Jacob/Jakob) both load — same raw_work_uri but different
    (variant, canonical) pairs."""
    p = tmp_path / "v.jsonl"
    append_variant_claims(
        p,
        [
            _make_claim(),
            _make_claim(variant_label="Johann Jacob", canonical_label="Johann Jakob"),
        ],
    )
    assert len(load_variant_claims(p)) == 2


def test_load_raises_on_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "v.jsonl"
    p.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"Bad JSON"):
        load_variant_claims(p)


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "v.jsonl"
    p.write_text(
        "\n" + _make_claim().model_dump_json() + "\n\n",
        encoding="utf-8",
    )
    assert len(load_variant_claims(p)) == 1


def test_truncate_resets_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "v.jsonl"
    append_variant_claims(p, [_make_claim()])
    truncate_sidecar(p)
    assert p.read_text(encoding="utf-8") == ""


def test_truncate_handles_missing_file(tmp_path: Path) -> None:
    """No raise when truncating a non-existent file — common no-op
    on first cascade run before the sidecar exists."""
    truncate_sidecar(tmp_path / "never-existed.jsonl")
    assert not (tmp_path / "never-existed.jsonl").exists()

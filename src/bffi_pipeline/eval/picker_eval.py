"""M9 picker eval harness — score the configured LLM picker against
a fixed JSONL of known-tricky cases.

Use when:

- Comparing picker LLM models (e.g. Qwen3-8B vs Qwen3-32B vs
  Qwen3.6-27B) without running the 30+ min full-chain smoke.
- Pinning known-tricky cases (Hakala-the-baritone,
  Koivisto-the-zoologist, etc.) as regression tests for prompt or
  model changes.
- Generating a one-page comparison table for PR / committee review.

The harness reuses :class:`bffi_pipeline.stages.m9.picker.LangChainLLMPicker`
verbatim, so it tests the *real* prompt + JSON-mode setup, not a
simplified mock. A single ``--model`` / ``--base-url`` override is the
only thing changing between runs.

Each case carries:

- ``id`` — short slug
- ``description`` — what this case is testing
- ``request`` — ``EntityRequest`` fields (literal, kind, work_uri,
  optional work_context)
- ``candidates`` — list of ``AuthorityCandidate`` fields, optionally
  with the ``context`` dict that maps to :class:`CandidateContext`
- ``expected_decision`` — ``"chose"`` or ``"uncertain"``
- ``expected_uri`` — the right URI when ``expected_decision == "chose"``;
  ``None`` for the uncertain case
- ``notes`` — free-text explanation of why this is the truth

Scoring per case:

- ``decision_match``: ``pick.decision == expected_decision``
- ``uri_match``: ``pick.chosen_uri == expected_uri`` (only meaningful
  when both expected_decision and pick.decision are ``"chose"``)
- ``confidently_wrong``: ``pick.decision == "chose"`` but
  ``pick.chosen_uri != expected_uri`` AND ``pick.confidence >= 0.85``.
  This is the Hakala-style failure mode the eval is most worried
  about — the picker confidently picking the wrong candidate.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bffi_pipeline.stages.m9.candidate_context import CandidateContext
from bffi_pipeline.stages.m9.picker import LLMPicker, PickerDecision
from bffi_pipeline.stages.m9.schemas import (
    AuthorityCandidate,
    EntityRequest,
    WorkContext,
)

#: A pick is "confidently wrong" if the picker chose a URI different
#: from the expected URI AND reported confidence at or above this
#: threshold. The Hakala-class failure mode: high confidence + wrong
#: pick = the LLM has rationalised itself into an incorrect answer.
CONFIDENTLY_WRONG_CONFIDENCE_FLOOR = 0.85


@dataclass(frozen=True)
class EvalCase:
    """One picker eval case loaded from JSONL."""

    id: str
    description: str
    request: EntityRequest
    candidates: list[AuthorityCandidate]
    expected_decision: str  # "chose" | "uncertain"
    expected_uri: str | None
    notes: str = ""


@dataclass(frozen=True)
class EvalResult:
    """Per-case scoring."""

    case: EvalCase
    pick: PickerDecision
    decision_match: bool
    uri_match: bool
    confidently_wrong: bool
    latency_seconds: float


@dataclass
class EvalSummary:
    """Aggregate stats across an eval-set run."""

    total: int = 0
    decision_matches: int = 0
    uri_matches: int = 0
    confidently_wrong: int = 0
    avg_latency: float = 0.0
    max_latency: float = 0.0
    # Picks where the picker chain fell through to "uncertain" with
    # the stub-marker rationale (JSON-mode parse failed after retries).
    parse_failures: int = 0

    def render(self) -> str:
        def pct(n: int) -> str:
            return f"{100 * n / self.total:.0f}%" if self.total else "n/a"

        dec = f"decision-match={self.decision_matches}/{self.total} ({pct(self.decision_matches)})"
        uri = f"uri-match={self.uri_matches}/{self.total} ({pct(self.uri_matches)})"
        return (
            f"  N={self.total}  {dec}  {uri}  "
            f"confidently-wrong={self.confidently_wrong}  parse-fail={self.parse_failures}\n"
            f"  avg latency: {self.avg_latency:.1f}s  max: {self.max_latency:.1f}s"
        )


# --- Loading -------------------------------------------------------------


def _candidate_from_dict(payload: dict[str, Any]) -> AuthorityCandidate:
    """Build an :class:`AuthorityCandidate` (with optional context) from
    a JSON dict shape. Mirrors the audit-log row layout so cases can
    be hand-copied from ``picker-decisions.jsonl``."""
    context_payload = payload.get("context")
    context = None
    if context_payload is not None:
        context = CandidateContext(
            definition=context_payload.get("definition"),
            scope_note=context_payload.get("scope_note"),
            alt_labels=tuple(context_payload.get("alt_labels") or ()),
            broader_labels=tuple(context_payload.get("broader_labels") or ()),
            birth_date=context_payload.get("birth_date"),
            death_date=context_payload.get("death_date"),
            field_of_activity_labels=tuple(context_payload.get("field_of_activity_labels") or ()),
            occupation_labels=tuple(context_payload.get("occupation_labels") or ()),
        )
    return AuthorityCandidate(
        uri=payload["uri"],
        pref_label=payload["pref_label"],
        source_vocabulary=payload["source_vocabulary"],
        lexical_similarity=float(payload["lexical_similarity"]),
        context=context,
    )


def _work_context_from_dict(payload: dict[str, Any] | None) -> WorkContext | None:
    if payload is None:
        return None
    return WorkContext(
        title=payload.get("title"),
        language=payload.get("language"),
        contributors=tuple(payload.get("contributors") or ()),
        sibling_subjects=tuple(payload.get("sibling_subjects") or ()),
        classification=tuple(payload.get("classification") or ()),
        year=payload.get("year"),
    )


def load_cases(path: Path) -> list[EvalCase]:
    """Read ``path`` as JSONL and yield one :class:`EvalCase` per line.

    Skips blank lines and ``#``-prefixed lines so the file can carry
    comments inline with cases.
    """
    cases: list[EvalCase] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        req_payload = d["request"]
        request = EntityRequest(
            work_uri=req_payload["work_uri"],
            literal=req_payload["literal"],
            kind=req_payload["kind"],
            predicate_uri=req_payload.get("predicate_uri"),
            work_context=_work_context_from_dict(req_payload.get("work_context")),
        )
        candidates = [_candidate_from_dict(c) for c in d["candidates"]]
        cases.append(
            EvalCase(
                id=d["id"],
                description=d.get("description", ""),
                request=request,
                candidates=candidates,
                expected_decision=d["expected_decision"],
                expected_uri=d.get("expected_uri"),
                notes=d.get("notes", ""),
            )
        )
    return cases


# --- Scoring -------------------------------------------------------------


def score_one(case: EvalCase, picker: LLMPicker) -> EvalResult:
    """Run the picker against one case and score the outcome."""
    started = time.monotonic()
    pick = picker.pick(request=case.request, candidates=case.candidates)
    elapsed = time.monotonic() - started

    decision_match = pick.decision == case.expected_decision
    uri_match = pick.chosen_uri == case.expected_uri
    confidently_wrong = (
        pick.decision == "chose"
        and pick.chosen_uri != case.expected_uri
        and pick.confidence >= CONFIDENTLY_WRONG_CONFIDENCE_FLOOR
    )
    return EvalResult(
        case=case,
        pick=pick,
        decision_match=decision_match,
        uri_match=uri_match,
        confidently_wrong=confidently_wrong,
        latency_seconds=elapsed,
    )


def score_set(cases: list[EvalCase], picker: LLMPicker) -> tuple[list[EvalResult], EvalSummary]:
    """Run every case and return per-case results + aggregate summary."""
    results: list[EvalResult] = []
    latencies: list[float] = []
    summary = EvalSummary(total=len(cases))
    for case in cases:
        r = score_one(case, picker)
        results.append(r)
        latencies.append(r.latency_seconds)
        if r.decision_match:
            summary.decision_matches += 1
        if r.uri_match:
            summary.uri_matches += 1
        if r.confidently_wrong:
            summary.confidently_wrong += 1
        # The picker's fall-through path emits a rationale prefixed with
        # "Picker fell through to uncertain after retries exhausted". When
        # the JSON-mode parse fails repeatedly, this is how it surfaces.
        if "fell through to uncertain" in (r.pick.rationale or ""):
            summary.parse_failures += 1
    if latencies:
        summary.avg_latency = sum(latencies) / len(latencies)
        summary.max_latency = max(latencies)
    return results, summary


# --- Rendering -----------------------------------------------------------


def render_table(results: list[EvalResult]) -> str:
    """Render per-case results as a fixed-width table."""
    rows: list[str] = []
    header = (
        f"  {'ID':30}  {'EXPECTED':10}  {'ACTUAL':10}  "
        f"{'DEC':3}  {'URI':3}  {'CONF':>5}  {'OK?':3}  {'LAT':>6}"
    )
    rows.append(header)
    rows.append("  " + "-" * 80)
    for r in results:
        ok = "✓" if (r.decision_match and r.uri_match) else "✗"
        dec_glyph = "✓" if r.decision_match else "✗"
        uri_glyph = "n/a" if r.case.expected_decision != "chose" else ("✓" if r.uri_match else "✗")
        rows.append(
            f"  {r.case.id:30}  "
            f"{r.case.expected_decision:10}  "
            f"{r.pick.decision:10}  "
            f"{dec_glyph:3}  "
            f"{uri_glyph:3}  "
            f"{r.pick.confidence:5.2f}  "
            f"{ok:3}  "
            f"{r.latency_seconds:>5.1f}s"
        )
    return "\n".join(rows)


__all__ = [
    "CONFIDENTLY_WRONG_CONFIDENCE_FLOOR",
    "EvalCase",
    "EvalResult",
    "EvalSummary",
    "load_cases",
    "render_table",
    "score_one",
    "score_set",
]

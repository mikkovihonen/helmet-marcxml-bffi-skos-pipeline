"""Unit tests for the canonical pipeline runner module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from bffi_pipeline import runner as runner_module
from bffi_pipeline.config import get_settings
from bffi_pipeline.runner import CANONICAL_STAGES, run_pipeline
from bffi_pipeline.stages.observability import (
    StageEventEmitter,
    set_active_emitter,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Same isolation pattern as test_cli.py — clear settings cache + active emitter."""
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")
    get_settings.cache_clear()
    set_active_emitter(None)
    yield
    get_settings.cache_clear()
    set_active_emitter(None)


@pytest.fixture
def active_emitter(tmp_path: Path) -> Iterator[StageEventEmitter]:
    """Register an emitter that writes to a sidecar so tests can read events back."""
    sidecar = tmp_path / "stage-events.jsonl"
    emitter = StageEventEmitter(sidecar_path=sidecar, run_uuid="testrun")
    set_active_emitter(emitter)
    yield emitter
    set_active_emitter(None)


def _read_events(emitter: StageEventEmitter) -> list[dict]:
    assert emitter.sidecar_path is not None
    if not emitter.sidecar_path.exists():
        return []
    return [json.loads(line) for line in emitter.sidecar_path.read_text().splitlines() if line]


@pytest.fixture
def stub_all_stages(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    """Replace every dispatcher with a stub that records its call kwargs.

    Returns a dict mapping stage -> list of recorded call kwargs.
    """
    calls: dict[str, list[dict]] = {stage: [] for stage in CANONICAL_STAGES}

    def make_stub(stage: str):
        def _stub(**kwargs):
            calls[stage].append(kwargs)

        return _stub

    monkeypatch.setattr(
        runner_module,
        "_DISPATCHERS",
        {stage: make_stub(stage) for stage in CANONICAL_STAGES},
    )
    return calls


def test_run_pipeline_dispatches_canonical_stages_in_order(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    summary = run_pipeline(input_dir=tmp_path)

    # Every stage ran once.
    for stage in CANONICAL_STAGES:
        assert len(stub_all_stages[stage]) == 1, f"{stage} not dispatched"

    # Summary order matches CANONICAL_STAGES.
    assert [o.stage for o in summary.outcomes] == list(CANONICAL_STAGES)
    assert all(o.status == "completed" for o in summary.outcomes)


def test_run_pipeline_emits_plan_event_with_full_stage_list(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    run_pipeline(input_dir=tmp_path, description="unit test")

    events = _read_events(active_emitter)
    plans = [e for e in events if e["event"] == "plan"]
    assert len(plans) == 1
    assert plans[0]["extra"]["stages"] == list(CANONICAL_STAGES)
    assert plans[0]["extra"]["description"] == "unit test"


def test_run_pipeline_dispatches_export_when_included_in_stages(
    monkeypatch: pytest.MonkeyPatch,
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    """``export`` lives in ``_DISPATCHERS`` + ``STAGE_PHASES`` but not in
    ``CANONICAL_STAGES`` — it's opt-in via ``--stages``. Verify the
    runner can dispatch it without error when the operator includes it."""
    calls: list[str] = []

    def make_stub(stage_name: str):
        def _stub(**_k):
            calls.append(stage_name)

        return _stub

    monkeypatch.setattr(
        runner_module,
        "_DISPATCHERS",
        {s: make_stub(s) for s in runner_module._DISPATCHERS} | {"export": make_stub("export")},
    )
    run_pipeline(
        input_dir=tmp_path,
        stages=("m2", "export"),
    )
    assert "export" in calls
    # Sanity: STAGE_PHASES has an entry so the dashboard's pending bar fires.
    assert "export" in runner_module.STAGE_PHASES


def test_run_pipeline_plan_event_includes_stage_phases_for_m9(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    """``stage_phases`` extra lets the exporter pre-create
    ``bffi_stage_phase_planned`` gauges so the dashboard renders 0%
    pending bars for M9's three phases before M9 actually starts."""
    run_pipeline(input_dir=tmp_path)

    events = _read_events(active_emitter)
    plan = next(e for e in events if e["event"] == "plan")
    stage_phases = plan["extra"]["stage_phases"]
    assert stage_phases["m9"] == ["phase1", "phase2", "phase3"]
    # Single-phase stages still declare their implicit "_" so the
    # exporter can pre-create pending markers for them too.
    assert stage_phases["m3"] == ["_"]
    assert stage_phases["skosify"] == ["_"]


def test_run_pipeline_stage_phases_filtered_to_explicit_stages(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    """When the operator runs a subset of stages, ``stage_phases``
    declares only the phases of the included stages."""
    run_pipeline(input_dir=tmp_path, stages=("m8", "m9"), from_stage="m8")

    events = _read_events(active_emitter)
    plan = next(e for e in events if e["event"] == "plan")
    stage_phases = plan["extra"]["stage_phases"]
    assert set(stage_phases.keys()) == {"m8", "m9"}


def test_run_pipeline_emits_skipped_event_for_each_skipped_stage(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    run_pipeline(
        input_dir=tmp_path,
        skip_stages=frozenset({"skosify", "load"}),
    )

    events = _read_events(active_emitter)
    skipped = [e for e in events if e["event"] == "skipped"]
    assert {e["stage"] for e in skipped} == {"skosify", "load"}
    for ev in skipped:
        assert ev["extra"]["reason"] == "operator-skipped"

    # Skipped dispatchers were not invoked.
    assert stub_all_stages["skosify"] == []
    assert stub_all_stages["load"] == []


def test_from_stage_skips_earlier_stages_with_resume_reason(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    summary = run_pipeline(input_dir=tmp_path, from_stage="m6")

    events = _read_events(active_emitter)
    skipped = {e["stage"]: e for e in events if e["event"] == "skipped"}

    # Stages strictly before m6 were skipped with resume-from-stage.
    assert set(skipped.keys()) == {"m2", "m3", "m5"}
    for ev in skipped.values():
        assert ev["extra"]["reason"] == "resume-from-stage"

    # m6 onwards actually dispatched.
    for stage in ("m6", "m8", "m9", "skosify", "load"):
        assert len(stub_all_stages[stage]) == 1

    # Summary records the right shape.
    statuses = {o.stage: o.status for o in summary.outcomes}
    assert statuses["m2"] == "skipped"
    assert statuses["m6"] == "completed"


def test_force_stages_passes_force_only_to_supporting_dispatchers(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    run_pipeline(
        input_dir=tmp_path,
        force_stages=frozenset({"m3", "m6", "m8"}),
    )

    # Force-supporting stages received force=True.
    assert stub_all_stages["m3"][0] == {"force": True}
    assert stub_all_stages["m6"][0] == {"force": True}
    # m8 has no force kwarg — dispatcher called with no kwargs regardless.
    assert stub_all_stages["m8"][0] == {}
    # Stages not in force_stages got force=False.
    assert stub_all_stages["m5"][0] == {"force": False}
    assert stub_all_stages["skosify"][0] == {"force": False}


def test_failed_stage_records_failure_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def boom(**kwargs):
        raise RuntimeError("kaboom")

    def ok(**kwargs):
        calls.append("ran")

    monkeypatch.setattr(
        runner_module,
        "_DISPATCHERS",
        {
            "m2": ok,
            "m3": ok,
            "m5": boom,
            "m6": ok,
            "m8": ok,
            "m9": ok,
            "skosify": ok,
            "load": ok,
        },
    )

    with pytest.raises(RuntimeError, match="kaboom"):
        run_pipeline(input_dir=tmp_path)

    # Three ok calls (m2, m3) before m5 blew up — m5 itself raised so didn't append.
    assert len(calls) == 2


def test_failed_stage_emits_failed_event_with_error_type_and_message(
    monkeypatch: pytest.MonkeyPatch,
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    """The runner emits a ``failed`` event before re-raising so the
    dashboard can distinguish "stuck mid-run" from "raised + caught".
    error_type carries the exception class; message carries str(exc)."""
    monkeypatch.setattr(
        runner_module,
        "_DISPATCHERS",
        {stage: (lambda **_k: None) for stage in CANONICAL_STAGES}
        | {"m6": (lambda **_k: (_ for _ in ()).throw(TimeoutError("LLM timed out at pair 42")))},
    )

    with pytest.raises(TimeoutError):
        run_pipeline(input_dir=tmp_path)

    events = _read_events(active_emitter)
    failed = [e for e in events if e["event"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["stage"] == "m6"
    assert failed[0]["extra"]["error_type"] == "TimeoutError"
    assert "LLM timed out" in failed[0]["extra"]["message"]


def test_failed_event_message_truncated_to_240_chars(
    monkeypatch: pytest.MonkeyPatch,
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    """Massive exception messages (e.g. a captured subprocess stderr
    dump) get truncated so the stage-events.jsonl rows stay small."""
    long_msg = "x" * 500

    def _raise_long(**_k):
        raise RuntimeError(long_msg)

    monkeypatch.setattr(
        runner_module,
        "_DISPATCHERS",
        {stage: (lambda **_k: None) for stage in CANONICAL_STAGES} | {"m2": _raise_long},
    )

    with pytest.raises(RuntimeError):
        run_pipeline(input_dir=tmp_path)

    events = _read_events(active_emitter)
    failed_msg = next(e for e in events if e["event"] == "failed")["extra"]["message"]
    # Truncated to MAX_TRUNCATED_LEN chars + the trailing ellipsis.
    max_truncated_len = 240
    assert len(failed_msg) == max_truncated_len + 1
    assert failed_msg.endswith("…")


def test_missing_input_dir_raises_when_m2_first_and_not_skipped() -> None:
    with pytest.raises(ValueError, match="m2 is the first planned stage"):
        run_pipeline(input_dir=None)


def test_missing_input_dir_ok_when_m2_skipped(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
) -> None:
    summary = run_pipeline(input_dir=None, from_stage="m3")

    # m2 was skipped, no input_dir needed.
    statuses = {o.stage: o.status for o in summary.outcomes}
    assert statuses["m2"] == "skipped"
    assert statuses["m3"] == "completed"


def test_from_stage_not_in_stages_list_raises() -> None:
    with pytest.raises(ValueError, match="not in stages list"):
        run_pipeline(input_dir=Path("/tmp"), from_stage="nonsense")


def test_summary_render_includes_status_glyphs(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    summary = run_pipeline(input_dir=tmp_path, skip_stages=frozenset({"load"}))
    out = summary.render()
    assert "✓ m2" in out
    assert "· load" in out


def test_stage_outcome_records_elapsed_seconds_for_completed(
    stub_all_stages: dict[str, list[dict]],
    active_emitter: StageEventEmitter,
    tmp_path: Path,
) -> None:
    summary = run_pipeline(input_dir=tmp_path, stages=("m2",))
    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].stage == "m2"
    assert summary.outcomes[0].status == "completed"
    assert summary.outcomes[0].elapsed_seconds >= 0

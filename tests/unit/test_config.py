"""Unit tests for ``bffi_pipeline.config.Settings`` (P-32 Phase E).

Pins the canonical `data_dir = runs_root / run_uuid` resolution
when the operator doesn't set `BFFI_DATA_DIR` explicitly, the
explicit-override escape hatch, and the startup-log echo that
distinguishes the two cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bffi_pipeline.cli import _init_observability
from bffi_pipeline.config import Settings
from bffi_pipeline.stages.observability import set_active_emitter


def test_settings_data_dir_defaults_to_runs_root_slash_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``BFFI_DATA_DIR`` env var → ``data_dir = runs_root / run_uuid``.

    This is the post-P-32 Phase E canonical resolution: every new run
    lands at a uuid-keyed location under the configured runs root,
    making the dirname the run's unambiguous identity.
    """
    monkeypatch.delenv("BFFI_DATA_DIR", raising=False)
    monkeypatch.delenv("BFFI_RUN_UUID", raising=False)
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))

    # ``_env_file=None`` bypasses the repo's .env so the test exercises
    # the validator's behaviour cleanly. (The local .env may still have
    # a legacy ``BFFI_DATA_DIR=./data`` line that pre-dates Phase E.)
    settings = Settings(_env_file=None)

    # run_uuid was auto-generated (non-empty hex string).
    assert settings.run_uuid
    assert len(settings.run_uuid) >= 16  # uuid4().hex is 32 chars

    # data_dir is the canonical derived path.
    assert settings.data_dir == tmp_path / settings.run_uuid

    # The resolution went through the validator path, not the
    # operator-supplied path.
    assert "data_dir" not in settings.model_fields_set


def test_settings_data_dir_respects_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``BFFI_DATA_DIR=/x`` → ``data_dir == /x``.

    Pins the operator-side escape hatch — the env var stays as the
    single override knob for reproducing legacy run paths or running
    one-off side-by-side benches.
    """
    override = tmp_path / "explicit-override"
    monkeypatch.setenv("BFFI_DATA_DIR", str(override))
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path / "unused-runs-root"))

    # ``_env_file=None`` bypasses the repo's .env so the test exercises
    # the validator's behaviour cleanly. (The local .env may still have
    # a legacy ``BFFI_DATA_DIR=./data`` line that pre-dates Phase E.)
    settings = Settings(_env_file=None)

    assert settings.data_dir == override
    # The validator's `data_dir not in model_fields_set` check
    # reports the operator's intent — used by the CLI's startup-log
    # echo to mark the override case.
    assert "data_dir" in settings.model_fields_set


def test_startup_log_warns_on_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI startup-log echo distinguishes canonical from override.

    Pins the operator-facing diagnostic: a run launched with
    ``BFFI_DATA_DIR`` set produces a "(override via BFFI_DATA_DIR —
    non-canonical; …)" warning on stderr; a run with the env var
    unset produces "(canonical)".

    Drives ``_init_observability`` directly with two ``Settings``
    instances constructed with ``_env_file=None`` (so the repo's .env
    can't interfere), one with explicit ``data_dir`` and one without.
    Asserts against the captured stderr.
    """
    # Disable sidecar so the test doesn't write a manifest into tmp_path —
    # we only care about the startup-log lines.
    monkeypatch.setenv("BFFI_OBSERVABILITY_SIDECAR", "none")

    # Branch 1: no BFFI_DATA_DIR → canonical marker.
    monkeypatch.delenv("BFFI_DATA_DIR", raising=False)
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path / "canonical-runs-root"))
    settings_canonical = Settings(_env_file=None)
    capsys.readouterr()  # drain prior captures
    _init_observability(settings_canonical)
    err = capsys.readouterr().err
    assert "(canonical)" in err, f"Expected canonical marker; stderr was: {err!r}"
    assert "override via BFFI_DATA_DIR" not in err, "Override warning fired in the canonical case"
    set_active_emitter(None)

    # Branch 2: BFFI_DATA_DIR set → override marker + warning.
    override = tmp_path / "explicit-override"
    monkeypatch.setenv("BFFI_DATA_DIR", str(override))
    settings_override = Settings(_env_file=None)
    capsys.readouterr()
    _init_observability(settings_override)
    err = capsys.readouterr().err
    assert "(override via BFFI_DATA_DIR" in err, f"Expected override marker; stderr was: {err!r}"
    assert "non-canonical" in err
    set_active_emitter(None)


def test_settings_explicit_run_uuid_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BFFI_RUN_UUID`` set explicitly survives the validator.

    Supports replay scenarios where the operator pins a uuid across
    nested invocations.
    """
    monkeypatch.delenv("BFFI_DATA_DIR", raising=False)
    monkeypatch.setenv("BFFI_RUN_UUID", "pinned-uuid-12345")
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))

    # ``_env_file=None`` bypasses the repo's .env so the test exercises
    # the validator's behaviour cleanly. (The local .env may still have
    # a legacy ``BFFI_DATA_DIR=./data`` line that pre-dates Phase E.)
    settings = Settings(_env_file=None)
    assert settings.run_uuid == "pinned-uuid-12345"
    assert settings.data_dir == tmp_path / "pinned-uuid-12345"


def test_settings_empty_run_uuid_env_still_triggers_fresh_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-only ``BFFI_RUN_UUID`` is treated as unset.

    Operators occasionally trip on quoting (``BFFI_RUN_UUID=" "``);
    the validator's ``strip()`` guard catches it.
    """
    monkeypatch.delenv("BFFI_DATA_DIR", raising=False)
    monkeypatch.setenv("BFFI_RUN_UUID", "   ")
    monkeypatch.setenv("BFFI_RUNS_ROOT", str(tmp_path))

    # ``_env_file=None`` bypasses the repo's .env so the test exercises
    # the validator's behaviour cleanly. (The local .env may still have
    # a legacy ``BFFI_DATA_DIR=./data`` line that pre-dates Phase E.)
    settings = Settings(_env_file=None)
    # Got a fresh uuid (32-hex-char uuid4 form).
    assert len(settings.run_uuid) == 32
    assert all(c in "0123456789abcdef" for c in settings.run_uuid)

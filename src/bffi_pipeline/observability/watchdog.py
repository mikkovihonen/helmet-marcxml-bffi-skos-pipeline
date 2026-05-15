"""Structured event emission for the LLM-call watchdog (plans P-03, P-10).

Watchdog events fire when an LLM call's per-call wall-time budget
(``LLM_CALL_TIMEOUT_SECONDS``) is exceeded, or — for M6 — when the
cumulative per-pair budget is exceeded, or — for M9 — when the
per-field budget is exceeded. The retry behaviour itself lives in
:func:`bffi_pipeline.stages.m6.judge_pair`'s and
:class:`bffi_pipeline.stages.m9.runner.LangChainLLMPicker`'s
existing connection-error retry stacks; this module is the
observability surface that lets an operator running an unattended
overnight batch see watchdog activity in real time *and* audit it
after the fact.

Events go to two destinations:

- **stderr**, each line prefixed ``WATCHDOG_EVENT `` so the existing
  pipeline-log tail / Monitor filter picks them up with a
  single-token regex broadening
  (``^(STAGE_|PIPELINE_|WATCHDOG_EVENT)``).
- **A sidecar JSONL** at ``<BFFI_DATA_DIR>/watchdog-events.jsonl``,
  one JSON object per line, for post-run audit (count events per
  pair, distribution by event type, etc.).

Event vocabulary (one line per call):

- ``timeout``               — a single LLM call exceeded
                              ``LLM_CALL_TIMEOUT_SECONDS``.
- ``retry``                 — the cascade re-attempts the same pair
                              on the same model after a timeout.
- ``escalate``              — primary-model retries exhausted;
                              cascade moves to the fallback model.
- ``give_up``               — fallback-model retries also
                              exhausted; the pair lands as
                              ``decision="uncertain"`` with the
                              ``bffi-prov:stage = "watchdog-aborted"``
                              marker.
- ``pair_budget_exceeded``  — cumulative wall time for one pair
                              (across all cascade tiers + retries)
                              exceeded ``LLM_PAIR_TIMEOUT_SECONDS``.
                              The pair is abandoned with no further
                              retries; same ``watchdog-aborted``
                              provenance stage as ``give_up``.
                              M6-side.
- ``field_budget_exceeded`` — cumulative wall time for one M9
                              reconciliation field (one
                              ``(work, predicate, literal)`` tuple)
                              exceeded
                              ``LLM_M9_FIELD_TIMEOUT_SECONDS``. The
                              field is abandoned, marked
                              ``bffi-prov:stage = "watchdog-aborted"``,
                              and falls through to tier-3 (highest-
                              lexical candidate + needs-review).
                              M9-side analogue of
                              ``pair_budget_exceeded``.

No shared state, no module-level config — the caller passes every
field explicitly so the function stays trivially testable.

The ``pair_id`` parameter is semantically overloaded: M6 callers
pass a ``"raw_a+raw_b"`` pair identifier; M9 callers pass a
``"<work_uri>|<predicate>|<literal>"`` field identifier. The function
itself treats it as an opaque string key.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from bffi_pipeline.observability.events import emit_if_active

WatchdogEvent = Literal[
    "timeout",
    "retry",
    "escalate",
    "give_up",
    "pair_budget_exceeded",
    "field_budget_exceeded",
]

#: stderr prefix; pipeline.log tails / Monitor filters match on this
#: literal to surface watchdog activity alongside ``STAGE_`` markers.
WATCHDOG_STDERR_PREFIX: Final[str] = "WATCHDOG_EVENT "


def emit_watchdog_event(
    *,
    pair_id: str,
    event: WatchdogEvent,
    model_name: str,
    elapsed_seconds: float,
    retry_n: int,
    sidecar_path: Path | None = None,
) -> None:
    """Emit one structured event to stderr and (optionally) the sidecar.

    ``sidecar_path`` is ``None`` in unit tests + low-stakes call sites
    that only want the stderr surface. Production code paths pass
    ``<BFFI_DATA_DIR>/watchdog-events.jsonl``.

    The stderr line carries the ``WATCHDOG_EVENT `` prefix so that
    existing log-grep tooling can extract events without inventing a
    second log file. The sidecar carries the same JSON payload
    without the prefix — pure JSONL, parseable end-to-end with a
    one-liner.
    """
    payload = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pair_id": pair_id,
        "event": event,
        "model": model_name,
        "elapsed_s": round(elapsed_seconds, 3),
        "retry_n": retry_n,
    }
    line = json.dumps(payload, separators=(",", ":"))
    print(f"{WATCHDOG_STDERR_PREFIX}{line}", file=sys.stderr, flush=True)
    if sidecar_path is not None:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        with sidecar_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    # P-11 Phase A: forward a copy to the active stage-event emitter
    # so the unified status / dashboard surfaces watchdog activity
    # alongside per-stage progress. Operators can still tail
    # ``watchdog-events.jsonl`` for forensic audit (where the
    # standalone shape is committed); ``stage-events.jsonl`` carries
    # the same payload under ``event="watchdog"``.
    emit_if_active(
        stage="watchdog",
        event="watchdog",
        extra=payload,
    )


__all__ = ["WATCHDOG_STDERR_PREFIX", "WatchdogEvent", "emit_watchdog_event"]

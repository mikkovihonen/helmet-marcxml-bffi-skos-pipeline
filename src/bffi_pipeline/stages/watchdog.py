"""Structured event emission for the M6 LLM-call watchdog (plan P-03).

Watchdog events fire when an LLM call's per-call wall-time budget
(``LLM_CALL_TIMEOUT_SECONDS``) is exceeded. The retry behaviour
itself lives in :func:`bffi_pipeline.stages.judge.judge_pair`'s
existing connection-error retry stack; this module is the
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

- ``timeout``    — a single LLM call exceeded the budget.
- ``retry``      — the cascade re-attempts the same pair on the
                   same model after a timeout.
- ``escalate``   — primary-model retries exhausted; cascade moves
                   to the fallback model.
- ``give_up``    — fallback-model retries also exhausted; the pair
                   lands as ``decision="uncertain"`` with the
                   ``bffi-prov:stage = "watchdog-aborted"`` marker.

No shared state, no module-level config — the caller passes every
field explicitly so the function stays trivially testable.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

WatchdogEvent = Literal["timeout", "retry", "escalate", "give_up"]

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


__all__ = ["WATCHDOG_STDERR_PREFIX", "WatchdogEvent", "emit_watchdog_event"]

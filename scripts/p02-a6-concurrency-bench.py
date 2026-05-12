"""P-02 A6 — concurrency sweep against the running mlx-lm server.

Measures throughput (pairs/min) and peak resident memory of the
``mlx_lm.server`` process across concurrency settings ``{1, 4, 8, 16,
32}``. Bypasses M5's escalate-band candidate file (no 1000-pair sample
available on this machine yet) by replaying the 17 gold-set cases as
``N=200`` synthetic ``(record_a, record_b)`` invocations.

Each invocation goes through the same ``_build_chain`` code path the
production cascade uses (including ``json_mode_instruction``), so the
numbers reflect end-to-end primary-judge call latency, not raw server
latency. Cascade escalation is *not* exercised — the bench drives only
the primary chain, so the 32B fallback stays idle.

Output: per-concurrency throughput line on stdout + a JSON summary at
``eval-runs/p02-a6-concurrency-sweep.json``.

Run via:

    set -a; . ./.env.mlx-lm; set +a
    uv run python scripts/p02-a6-concurrency-bench.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bffi_pipeline.stages.judge import _build_chain


N_PAIRS = 200
CONCURRENCIES = (1, 4, 8, 16, 32)


def _render_record(rec: dict) -> str:
    """Render a gold-set ``record_a``/``record_b`` block as the
    pipeline's M6 prompt expects — a multiline ``key="value"`` list."""
    fields = [
        ("creator", rec.get("creator")),
        ("preferred_title", rec.get("title")),
        ("expression_language", rec.get("language")),
        ("original_language", rec.get("translated_from_lang")),
        ("content_type", rec.get("content_type")),
        ("publication_year", rec.get("year")),
        ("helmet_bib_id", rec.get("helmet_bib_id")),
    ]
    return "\n".join(f'  {k}="{v}"' for k, v in fields if v is not None)


def build_payloads(gold_path: Path, n: int) -> list[dict]:
    with open(gold_path) as f:
        cases = [json.loads(line) for line in f]
    k = len(cases)
    payloads = []
    for i in range(n):
        a = cases[i % k]
        b = cases[(i // k + 1 + i) % k]  # rotate so a != b
        if a["id"] == b["id"]:
            b = cases[(i + 3) % k]
        payloads.append(
            {
                "record_a": _render_record(a["record_a"]),
                "record_b": _render_record(b["record_b"]),
                "sim": 0.50 + (i % 50) / 100.0,
            }
        )
    return payloads


def server_pids() -> list[str]:
    try:
        out = subprocess.check_output(["pgrep", "-f", "mlx_lm.*server"]).decode()
        return [p for p in out.split() if p]
    except subprocess.CalledProcessError:
        return []


def server_memory_mb() -> int | None:
    pids = server_pids()
    if not pids:
        return None
    total_kb = 0
    for pid in pids:
        try:
            out = subprocess.check_output(["ps", "-p", pid, "-o", "rss="]).decode().strip()
            total_kb += int(out)
        except (subprocess.CalledProcessError, ValueError):
            continue
    return total_kb // 1024


async def memory_watcher(stop_event: asyncio.Event, peak: list[int]) -> None:
    while not stop_event.is_set():
        m = server_memory_mb()
        if m is not None:
            peak[0] = max(peak[0], m)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def run_one(chain, payload: dict) -> tuple[bool, float]:
    t0 = time.monotonic()
    try:
        await chain.ainvoke(payload)
        ok = True
    except Exception:
        ok = False
    return ok, time.monotonic() - t0


async def sweep(chain, payloads: list[dict], concurrency: int) -> dict:
    sem = asyncio.Semaphore(concurrency)

    async def bounded(p: dict) -> tuple[bool, float]:
        async with sem:
            return await run_one(chain, p)

    peak = [server_memory_mb() or 0]
    stop = asyncio.Event()
    watcher = asyncio.create_task(memory_watcher(stop, peak))

    t0 = time.monotonic()
    results = await asyncio.gather(*(bounded(p) for p in payloads))
    wall = time.monotonic() - t0

    stop.set()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher

    ok = sum(1 for r in results if r[0])
    latencies = sorted(r[1] for r in results)
    median = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    return {
        "concurrency": concurrency,
        "wall_seconds": round(wall, 2),
        "ok": ok,
        "total": len(payloads),
        "pairs_per_min": round(len(payloads) / wall * 60, 1),
        "median_latency_s": round(median, 3),
        "p95_latency_s": round(p95, 3),
        "peak_server_mb": peak[0],
    }


def main() -> int:
    for key in ("LLM_BASE_URL_PRIMARY", "LLM_MODEL_PRIMARY"):
        if not os.environ.get(key):
            print(f"ERROR: {key} not set in env. Source .env.mlx-lm first.", file=sys.stderr)
            return 2

    chain = _build_chain(
        model_name=os.environ["LLM_MODEL_PRIMARY"],
        base_url=os.environ["LLM_BASE_URL_PRIMARY"],
        api_key=os.environ.get("LLM_API_KEY", "mlx-lm"),
    )

    payloads = build_payloads(Path("gold/gold.jsonl"), n=N_PAIRS)
    print(f"Loaded {len(payloads)} synthetic pair payloads from gold/gold.jsonl")
    print(f"Server: {os.environ['LLM_BASE_URL_PRIMARY']}  model: {os.environ['LLM_MODEL_PRIMARY']}")
    print()
    print(f"{'concurrency':>11}  {'wall_s':>7}  {'ok/total':>10}  {'pairs/min':>10}  {'med_s':>7}  {'p95_s':>7}  {'peak_MB':>8}")

    summary = {"n_pairs": N_PAIRS, "server_url": os.environ["LLM_BASE_URL_PRIMARY"], "model": os.environ["LLM_MODEL_PRIMARY"], "runs": []}
    for c in CONCURRENCIES:
        row = asyncio.run(sweep(chain, payloads, c))
        summary["runs"].append(row)
        print(
            f"{row['concurrency']:>11}  {row['wall_seconds']:>7.1f}  "
            f"{row['ok']:>4}/{row['total']:<4}  {row['pairs_per_min']:>10.1f}  "
            f"{row['median_latency_s']:>7.3f}  {row['p95_latency_s']:>7.3f}  {row['peak_server_mb']:>8d}"
        )

    out = Path("eval-runs/p02-a6-concurrency-sweep.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nWritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

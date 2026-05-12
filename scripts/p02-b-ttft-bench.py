"""P-02 § B4 — TTFT bench for the mlx-lm server-side prefix cache.

Measures time-to-first-token (TTFT) for the M6 system prefix.
The first call after a server restart is the cold-cache reference;
calls 2..N hit the warm prefix cache and should show a sharply lower
TTFT — that's the Phase B acceptance signal (≥ 3× speedup).

Methodology:

- Use the real ``_M6_PROMPT_PREFIX_FULL`` as the system message so
  the cached bytes are exactly what production sends.
- Per-call user message varies (Pushkin / Tolkien / Morton stub
  pairs) so the suffix doesn't itself hit the cache; only the
  system prefix should benefit.
- Stream the response and clock the wall time from request-send to
  first content-bearing chunk. ``response_format={"type":"json_object"}``
  matches production.
- Run sequentially (no client concurrency) so per-call TTFT
  isn't muddied by queueing.

Pre-condition: the 8B server must have been **restarted before this
bench** so the prompt cache is cold for call #1. Run via::

    set -a; . ./.env.mlx-lm; set +a
    uv run python scripts/p02-b-ttft-bench.py

Output: per-call TTFT line on stdout, plus a summary at the end and
a JSON dump at ``eval-runs/p02-b-ttft-bench.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from openai import AsyncOpenAI

from bffi_pipeline.stages.judge import _M6_PROMPT_PREFIX_FULL


N_CALLS = 8


USER_PAYLOADS = [
    'creator="Pushkin, A.", title="Yevgeny Onegin"\nvs\ncreator="Puskin", title="Jevgeni Onegin"',
    'creator="Tolkien, J.R.R.", title="The Hobbit"\nvs\ncreator="Tolkien", title="Hobitti"',
    'creator="Morton, Kate", title="The Clockmaker\'s Daughter"\nvs\ncreator="Morton", title="Kellontekijän tytär"',
    'creator="Dostoevsky, F.", title="Crime and Punishment"\nvs\ncreator="Dostoyevskiy", title="Преступление и наказание"',
    'creator="Tolstoy, L.", title="War and Peace"\nvs\ncreator="Tolstoi", title="Sota ja rauha"',
    'creator="Saramago, J.", title="Blindness"\nvs\ncreator="Saramago", title="Cegueira"',
    'creator="Kafka, F.", title="The Trial"\nvs\ncreator="Kafka", title="Der Process"',
    'creator="Camus, A.", title="The Stranger"\nvs\ncreator="Camus", title="L\'Étranger"',
]


async def time_one(client: AsyncOpenAI, model: str, user: str) -> dict:
    t0 = time.monotonic()
    first_token_at: float | None = None
    last_token_at: float | None = None
    chunk_count = 0
    stream = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _M6_PROMPT_PREFIX_FULL},
            {"role": "user", "content": user},
        ],
        stream=True,
        temperature=0.0,
        max_tokens=120,
        response_format={"type": "json_object"},
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            now = time.monotonic()
            if first_token_at is None:
                first_token_at = now
            last_token_at = now
            chunk_count += 1
    end = time.monotonic()
    ttft = (first_token_at - t0) if first_token_at else None
    total = end - t0
    return {
        "ttft_s": round(ttft, 4) if ttft is not None else None,
        "total_s": round(total, 4),
        "chunks": chunk_count,
    }


async def main() -> int:
    for key in ("LLM_BASE_URL_PRIMARY", "LLM_MODEL_PRIMARY"):
        if not os.environ.get(key):
            print(f"ERROR: {key} not set. Source .env.mlx-lm first.", file=sys.stderr)
            return 2

    client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL_PRIMARY"], api_key="mlx-lm")
    model = os.environ["LLM_MODEL_PRIMARY"]

    print(f"Server: {os.environ['LLM_BASE_URL_PRIMARY']}  model: {model}")
    print(f"System prefix: {len(_M6_PROMPT_PREFIX_FULL)} chars (~{len(_M6_PROMPT_PREFIX_FULL) // 4} tokens approx)")
    print(f"Calls: {N_CALLS} sequential, streaming, response_format=json_object")
    print()
    print(f"{'call':>5}  {'ttft_s':>8}  {'total_s':>9}  {'chunks':>7}  user (first 40 chars)")

    results = []
    for i in range(N_CALLS):
        user = USER_PAYLOADS[i % len(USER_PAYLOADS)] + f"\n(run {i})"
        r = await time_one(client, model, user)
        results.append(r)
        print(f"{i:>5}  {r['ttft_s']!s:>8}  {r['total_s']!s:>9}  {r['chunks']:>7}  {user[:40]!s}")

    cold = results[0]
    warm = results[1:]
    warm_ttfts = [r["ttft_s"] for r in warm if r["ttft_s"] is not None]
    warm_median = statistics.median(warm_ttfts) if warm_ttfts else None

    print()
    print(f"  cold (call 0)  TTFT = {cold['ttft_s']} s")
    print(f"  warm (calls 1..{N_CALLS - 1}) median TTFT = {warm_median} s")
    if warm_median and cold["ttft_s"]:
        speedup = cold["ttft_s"] / warm_median
        print(f"  speedup factor: {speedup:.2f}x (Phase B acceptance: ≥ 3x)")

    out = {
        "prefix_chars": len(_M6_PROMPT_PREFIX_FULL),
        "n_calls": N_CALLS,
        "calls": results,
        "cold_ttft_s": cold["ttft_s"],
        "warm_median_ttft_s": warm_median,
        "speedup": (cold["ttft_s"] / warm_median) if (warm_median and cold["ttft_s"]) else None,
    }
    Path("eval-runs").mkdir(exist_ok=True)
    Path("eval-runs/p02-b-ttft-bench.json").write_text(json.dumps(out, indent=2))
    print(f"\nWritten: eval-runs/p02-b-ttft-bench.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

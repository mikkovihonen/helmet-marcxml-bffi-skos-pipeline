# Bogus results — all routed to Qwen3-8B

These four eval-picker JSON outputs (qwen3-8b, gemma-4-26B-A4B,
gemma-4-31B, gemma-4-26B-A4B-v5) all hit the same Qwen3-8B-4bit
mlx-lm server on port 8001 because:

- A pre-existing Qwen3-8B server was holding port 8001 (started
  outside this session).
- The shootout script started its own mlx-lm subprocesses with the
  Gemma model paths, but they bind-failed with
  ``OSError: [Errno 48] Address already in use`` — silently, because
  stderr was redirected to log files that were never inspected.
- The readiness curl hit the pre-existing Qwen3-8B server and
  returned 200 in 1 second every time.
- The eval ran 30 times against Qwen3-8B; the "different" rationales
  between runs were random LLM sampling variance, not different models.

Kept for forensic reference (the variance distribution itself is
interesting). DO NOT use as a model comparison. The corrected
shootout is in the parent directory.

The bug fix lives in ``scripts/eval-picker-shootout.sh``: refuses to
start when port is already in use, and verifies ``/v1/models`` reports
the requested model path before sending eval traffic.

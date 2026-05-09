"""Live reconciliation test against the local LLM + real Finto (M9 phase 2).

Marked ``requires_llm``: excluded from CI by ``-m "not requires_llm"``.
Runs on the user's M5 Max where Ollama / vllm-mlx is alive *and* the
machine has internet access to ``api.finto.fi``. Skipped automatically
if either prerequisite is missing.

The test seeds a small list of well-known creator literals lifted from
the curated 13 records (Pushkin / Mozart / Wilson / Morton) and runs
the cascade end-to-end against real KANTO. It asserts the obvious
high-confidence cases land on the lexical-direct path and that no
case silently picks an out-of-set URI.
"""

from __future__ import annotations

import os

import httpx
import pytest

from bffi_pipeline.stages.reconcile import (
    STAGE_FALLBACK,
    STAGE_LEXICAL,
    STAGE_LLM,
    STAGE_NO_CANDIDATE,
    EntityRequest,
    FintoSkosmosClient,
    LangChainLLMPicker,
    reconcile_one,
)

# --- Curated creator literals from the M5 Max gold sample ----------------

#: A small, deliberately easy set: each entry is a creator literal we
#: expect KANTO to know. The test asserts the cascade either resolves
#: them (lexical-direct or llm-pick) or honestly returns
#: no-candidate / fallback — never picks a hallucinated URI.
SEEDS: tuple[tuple[str, str], ...] = (
    # (work-uri-shaped tag, creator literal)
    ("http://urn.fi/URN:NBN:fi:bib:work:test-pushkin", "Puškin, Aleksandr,"),
    ("http://urn.fi/URN:NBN:fi:bib:work:test-mozart", "Mozart, Wolfgang Amadeus,"),
    ("http://urn.fi/URN:NBN:fi:bib:work:test-morton", "Morton, Kate,"),
    ("http://urn.fi/URN:NBN:fi:bib:work:test-wilson", "Wilson, Steven,"),
)


pytestmark = pytest.mark.requires_llm


def _finto_alive(http_client: httpx.Client) -> bool:
    try:
        response = http_client.get(
            "https://api.finto.fi/rest/v1/vocabularies",
            params={"lang": "en"},
            timeout=5.0,
        )
        return bool(response.is_success)
    except httpx.HTTPError:
        return False


def test_cascade_resolves_well_known_finnish_creators_against_kanto() -> None:
    if not os.environ.get("LLM_BASE_URL"):
        pytest.skip("LLM_BASE_URL not set; live reconcile test requires the local Qwen3 server.")

    http_client = httpx.Client(timeout=10.0)
    try:
        if not _finto_alive(http_client):
            pytest.skip(
                "api.finto.fi unreachable from this network; live reconcile test needs internet."
            )

        client = FintoSkosmosClient(http_client=http_client)
        picker = LangChainLLMPicker()

        print()  # leading newline so -s output reads cleanly
        for work_uri, literal in SEEDS:
            request = EntityRequest(work_uri=work_uri, literal=literal, kind="person")
            outcome = reconcile_one(
                request=request, client=client, fallback_client=None, picker=picker
            )
            stage_label = outcome.stage
            chosen = outcome.chosen_uri or "(no URI bound)"
            print(
                f"  {stage_label:<32s}  {literal!r:<40s}  -> {chosen} "
                f"(confidence {outcome.confidence:.2f})"
            )

            # Stage must be one of the four committed values.
            assert stage_label in {
                STAGE_LEXICAL,
                STAGE_LLM,
                STAGE_FALLBACK,
                STAGE_NO_CANDIDATE,
            }
            # Defence-in-depth: any bound URI must come from the candidate
            # pool the authority actually returned.
            if outcome.chosen_uri is not None:
                allowed = {c.uri for c in outcome.candidates}
                assert outcome.chosen_uri in allowed, (
                    f"Cascade bound {outcome.chosen_uri!r} for {literal!r}, "
                    "but it was not in the candidate pool — picker hallucinated."
                )
    finally:
        http_client.close()

"""M10 — Load + Skosify + post-load graph hygiene.

Unlike the other ``m<N>`` packages, M10 has no single umbrella ``runner.py``:
``cli.py`` / ``runner.py`` invoke its three sub-steps (``skosify_run``,
``load``, ``load_finto``) as an explicit chain, and ``fuseki_clear``
is a graph-hygiene helper used both inside this chain and externally
(``runs_reset``). Per the P-38 plan, M10 stays as four peer modules
in Layer 1; a follow-up plan can introduce an umbrella later if the
boundary clarifies the design.

Re-exporting the four submodules from the package so callers can write
``from bffi_pipeline.stages.m10 import load`` (vs. the longer
``from bffi_pipeline.stages.m10.load import …``).
"""

from bffi_pipeline.stages.m10 import fuseki_clear, load, load_finto, skosify_run

__all__ = [
    "fuseki_clear",
    "load",
    "load_finto",
    "skosify_run",
]

"""BFFI pipeline: MARCXML to BFFI Works/Expressions to Skosmos."""

import logging
import os

__version__ = "0.1.0"

# Silence rdflib's per-triple "does not look like a valid URI" and
# "Failed to convert Literal lexical form to value" warnings. Real
# corpus MARCXML (via marc2bibframe2) routinely produces malformed
# URI fragments and date literals — Pattern G/H sanitizers in M3 strip
# them before serialization, so the warnings are pure noise that
# crowds out the actual stage markers in ``pipeline.log``.
#
# Override with ``BFFI_RDFLIB_VERBOSE=1`` when debugging a new source
# format and you actually need to see what rdflib is choking on.
if not os.environ.get("BFFI_RDFLIB_VERBOSE"):
    logging.getLogger("rdflib.term").setLevel(logging.ERROR)

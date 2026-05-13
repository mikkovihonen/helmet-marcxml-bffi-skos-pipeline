"""Schema-instruction system message for ``method="json_mode"`` chains.

LangChain's ``with_structured_output(method="json_mode")`` only sets
``response_format={"type":"json_object"}`` on the request; it does *not*
inject a description of the schema into the prompt. Ollama tolerates this
because ``format=json`` is constrained decoding — the server forces JSON
output token-by-token regardless of what the prompt says. mlx-lm 0.31, by
contrast, treats ``response_format`` as a request-level hint with no
constrained-decoding fallback. The model therefore needs an explicit
prompt-side instruction to emit JSON, and the few-shot exemplars in
``prompts/*.txt`` show ``→ same_work, confidence 0.95.`` prose, which the
model otherwise copies verbatim.

This module produces that instruction deterministically from the Pydantic
schema — same byte-for-byte string for the same schema on every call, so
the prefix-cache benefit targeted by P-02 Phase B is preserved.

Discovered during P-02 § A5; see the plan's "Open issues" entry. Out-of-
scope alternative documented in ``docs/plans/proposed/prop-06-structured-output-backend.md``
as future structured-output-backend work (outlines / vllm-mlx).
"""

from __future__ import annotations

import json

from pydantic import BaseModel


def json_mode_instruction(schema: type[BaseModel]) -> str:
    """Build the schema-instruction system-message fragment.

    The returned string is escaped for ``ChatPromptTemplate`` f-string
    interpolation (curly braces doubled), so it can be concatenated into a
    system message without further processing.
    """
    schema_dict = schema.model_json_schema()
    instruction = (
        "Output format: respond with ONLY a JSON object matching the schema "
        "below. No prose outside the JSON, no markdown fences.\n"
        f"Schema: {json.dumps(schema_dict)}"
    )
    return instruction.replace("{", "{{").replace("}", "}}")

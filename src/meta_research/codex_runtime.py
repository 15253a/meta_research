from __future__ import annotations


CODEX_MODEL_REF = "gpt-5.6-sol"
CODEX_REASONING_EFFORT = "max"
CODEX_REASONING_EFFORT_CONFIG = (
    f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"'
)
CODEX_REASONING_EFFORT_BINDING = (
    f"codex-config:model_reasoning_effort={CODEX_REASONING_EFFORT}"
)

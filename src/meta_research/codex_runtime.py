from __future__ import annotations

import os

SUPPORTED_CODEX_MODEL_REFS = ("gpt-6.1-sol", "gpt-5.6-sol")
CODEX_MODEL_REF = os.environ.get("META_RESEARCH_CODEX_MODEL", "gpt-6.1-sol")
if CODEX_MODEL_REF not in SUPPORTED_CODEX_MODEL_REFS:
    raise ValueError(
        "META_RESEARCH_CODEX_MODEL must be gpt-6.1-sol or gpt-5.6-sol"
    )
CODEX_LOCKED_VERSION = "0.159.0"
# Model effort is distinct from the CLI preset, which also enables Ultra.
CODEX_REASONING_EFFORT = "max"
CODEX_ROOT_REASONING_PRESET = "ultra"
CODEX_COLLABORATION_MODE = "ultra"
CODEX_REASONING_EFFORT_CONFIG = f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"'
CODEX_ROOT_REASONING_PRESET_CONFIG = (
    f'model_reasoning_effort="{CODEX_ROOT_REASONING_PRESET}"'
)
CODEX_REASONING_EFFORT_BINDING = (
    f"codex-effective:reasoning.effort={CODEX_REASONING_EFFORT}"
)

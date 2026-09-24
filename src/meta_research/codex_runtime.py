from __future__ import annotations

CODEX_MODEL_REF = "gpt-6-sol"
CODEX_LOCKED_VERSION = "0.156.1"
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

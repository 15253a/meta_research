from __future__ import annotations

import os

# Visible models returned by Codex CLI 0.169.0's ``model/list`` endpoint on
# 2026-11-21. Keep the tuple ordered like the CLI so the Web selector and the
# command-line help expose the same choices.
SUPPORTED_CODEX_MODEL_REFS = (
    "gpt-6.2-sol",
    "gpt-6.2-astra",
    "gpt-6.1-terra",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
RESEARCH_STAGES = ("idea", "plan", "bundle", "target", "reasoning")
DEFAULT_STAGE_MODEL = "gpt-6.1-sol"
_ENV_CODEX_MODEL = os.environ.get("META_RESEARCH_CODEX_MODEL")
CODEX_MODEL_REF = _ENV_CODEX_MODEL or DEFAULT_STAGE_MODEL
if CODEX_MODEL_REF not in SUPPORTED_CODEX_MODEL_REFS:
    raise ValueError(
        "META_RESEARCH_CODEX_MODEL must be one of " + ", ".join(SUPPORTED_CODEX_MODEL_REFS)
    )
_ENV_DEFAULT_STAGE_MODEL = os.environ.get("META_RESEARCH_DEFAULT_STAGE_MODEL")
_NON_ASTRA_STAGE_MODEL = _ENV_DEFAULT_STAGE_MODEL or CODEX_MODEL_REF
if _NON_ASTRA_STAGE_MODEL not in SUPPORTED_CODEX_MODEL_REFS:
    raise ValueError(
        "META_RESEARCH_DEFAULT_STAGE_MODEL must be one of "
        + ", ".join(SUPPORTED_CODEX_MODEL_REFS)
    )
DEFAULT_STAGE_MODELS: dict[str, str] = {
    "idea": "gpt-6.2-astra",
    "plan": _NON_ASTRA_STAGE_MODEL,
    "bundle": _NON_ASTRA_STAGE_MODEL,
    "target": _NON_ASTRA_STAGE_MODEL,
    "reasoning": "gpt-6.2-astra",
}
if set(DEFAULT_STAGE_MODELS) != set(RESEARCH_STAGES) or any(
    model not in SUPPORTED_CODEX_MODEL_REFS for model in DEFAULT_STAGE_MODELS.values()
):
    raise ValueError(
        "DEFAULT_STAGE_MODELS must map every research stage to a supported Codex model"
    )
DEFAULT_CODEX_CLI_VERSION = "0.159.0"
SUPPORTED_CODEX_CLI_VERSIONS = (DEFAULT_CODEX_CLI_VERSION, "0.169.0")
CODEX_LOCKED_VERSION = os.environ.get("META_RESEARCH_CODEX_CLI_VERSION", DEFAULT_CODEX_CLI_VERSION)
if CODEX_LOCKED_VERSION not in SUPPORTED_CODEX_CLI_VERSIONS:
    raise ValueError(
        "META_RESEARCH_CODEX_CLI_VERSION must be one of " + ", ".join(SUPPORTED_CODEX_CLI_VERSIONS)
    )
# Model effort is distinct from the CLI preset, which also enables Ultra.
CODEX_REASONING_EFFORT = "max"
CODEX_ROOT_REASONING_PRESET = "ultra"
CODEX_COLLABORATION_MODE = "ultra"

# Luna currently exposes ``max`` as its highest reasoning level. Keep the
# established Ultra preset for models that advertise it and lower only the
# per-invocation reasoning preset for Luna models.
_MODELS_WITHOUT_ULTRA = frozenset({"gpt-6-luna", "gpt-5.6-luna"})


def codex_root_reasoning_preset(model_ref: str) -> str:
    return "max" if model_ref in _MODELS_WITHOUT_ULTRA else CODEX_ROOT_REASONING_PRESET


def codex_root_reasoning_preset_config(model_ref: str) -> str:
    return f'model_reasoning_effort="{codex_root_reasoning_preset(model_ref)}"'


CODEX_REASONING_EFFORT_CONFIG = f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"'
CODEX_ROOT_REASONING_PRESET_CONFIG = (
    f'model_reasoning_effort="{CODEX_ROOT_REASONING_PRESET}"'
)
CODEX_REASONING_SUMMARY = "concise"
CODEX_REASONING_SUMMARY_CONFIG = f'model_reasoning_summary="{CODEX_REASONING_SUMMARY}"'
CODEX_REASONING_EFFORT_BINDING = (
    f"codex-effective:reasoning.effort={CODEX_REASONING_EFFORT}"
)

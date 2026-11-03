"""Public, frozen Target input contract and bounded JSON result semantics."""
from __future__ import annotations

from pathlib import Path
import sys

from meta_research.bundle_protocol import (
    TARGET_METRIC_MAX_DEPTH, TargetMetricValue, valid_target_metric_value,
)
from meta_research.bundle_target_contract import measurement_contract_to_dict
from meta_research.owners.common import OwnerConflict
from meta_research.target_implementation_bundle import (
    IMPLEMENTATION_ENTRY_MAX_BYTES, IMPLEMENTATION_ENTRY_MAX_COUNT,
    IMPLEMENTATION_TOTAL_MAX_BYTES, IMPLEMENTATION_BUNDLE_MAX_BYTES,
)

def validate_target_result_tree(value: object, *, depth: int = 0) -> None:
    if depth > TARGET_METRIC_MAX_DEPTH:
        raise OwnerConflict("target_root_result_document_invalid")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise OwnerConflict("target_root_result_document_invalid")
            validate_target_result_tree(key, depth=depth + 1)
            validate_target_result_tree(item, depth=depth + 1)
    elif type(value) is list:
        for item in value:
            validate_target_result_tree(item, depth=depth + 1)
    elif type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise OwnerConflict("target_root_result_document_invalid") from error
    elif not valid_target_metric_value(value):
        raise OwnerConflict("target_root_result_document_invalid")


def target_execution_context(*, authority: object, target_ref: str,
                             graph_ref: str, target_spec_hash: str) -> dict[str, object]:
    """Project an already issuer-verified authority, rejecting mismatched scope."""
    # These imports happen after Owner composition, keeping the limit definitions
    # at their actual intake boundaries without a module import cycle.
    from meta_research.target_run_finalizer import (
        TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES,
    )
    if (authority is None or getattr(authority, "target_ref", None) != target_ref
            or getattr(authority, "graph_ref", None) != graph_ref
            or getattr(authority, "target_spec_hash", None) != target_spec_hash):
        raise OwnerConflict("target_measurement_domain_authority_invalid")
    return {
        "authority_ref": authority.authority_ref,
        "authority_hash": authority.authority_hash,
        "target_spec_hash": authority.target_spec_hash,
        "measurement_contract": measurement_contract_to_dict(authority.measurement_contract),
        "artifact_limits": {
            "artifact_bytes": None,
            "artifact_set_bytes": None,
            "result_document_bytes": TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES,
            "storage": "RM streams selected files and directory trees into managed storage; available disk space and actual I/O govern large data and checkpoints.",
            "supported_entries": "regular files and directories, no links",
            "legacy_implementation_bundle": {
                "directory_entry_count": IMPLEMENTATION_ENTRY_MAX_COUNT,
                "directory_file_bytes": IMPLEMENTATION_ENTRY_MAX_BYTES,
                "directory_uncompressed_bytes": IMPLEMENTATION_TOTAL_MAX_BYTES,
                "directory_bundle_bytes": IMPLEMENTATION_BUNDLE_MAX_BYTES,
            },
        },
        "result_encoding": "UTF-8 JSON; source bytes retained, semantic hash normalized",
        "result_schema_policy": "Initial result_schema guides structure; evolved domain fields and types are allowed. Protocol metric keys, exact identities, provenance, valid JSON and finite numeric safety remain binding.",
    }


def target_execution_skill_text() -> str:
    """Point the business turn at the role method maintained by the Harness."""
    path = Path(__file__).with_name("skills") / "target-execution" / "SKILL.md"
    return (f"Skill source: {path.resolve()}\n"
            "角色方法由 Harness 指令维持；交接正式工作时读 references/formal-work.md，"
            "选择数据与保存原件时读 references/data-preservation.md。"
            "Resolve role reference links relative to this source directory.")


def target_execution_reading_tools(workspace_directory: Path) -> dict[str, object]:
    python = str(Path(sys.executable).absolute())
    read_manifest = (
        "import hashlib,json,pathlib,sys\n"
        "p=json.loads(pathlib.Path(sys.argv[1]).read_bytes())\n"
        "m=pathlib.Path(p['read_only_manifest_path'])\n"
        "if p.get('schema_ref') != 'meta-research/target-root-input-pointer/v1' or not m.is_absolute(): raise ValueError('invalid frozen manifest pointer')\n"
        "b=m.read_bytes()\n"
        "if hashlib.sha256(b).hexdigest() != p['manifest_sha256']: raise ValueError('frozen manifest hash mismatch')\n"
        "sys.stdout.buffer.write(b)"
    )
    return {
        "project_python": python,
        "read_frozen_manifest_argv": [python, "-c", read_manifest, str(workspace_directory / "inputs" / "manifest.json")],
        "usage": "Use this absolute project Python entry without relying on PATH. The manifest command follows inputs/manifest.json and verifies the frozen manifest hash before returning its original bytes. Pass argv as separate arguments; shell use requires quoting each argument for that shell. Other research environments must be selected from the accepted Environment bindings.",
    }


def target_execution_reading_prompt(prompt: str, workspace_directory: Path) -> str:
    if not (workspace_directory / "inputs" / "manifest.json").is_file():
        return prompt
    from meta_research.owners.common import canonical_json
    return "Execution reading tools:\n" + canonical_json(target_execution_reading_tools(workspace_directory)) + "\n" + prompt

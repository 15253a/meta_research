"""Public, frozen Target input contract and bounded JSON result semantics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from meta_research.bundle_protocol import (
    TARGET_METRIC_MAX_DEPTH, TargetMetricValue, valid_target_metric_value,
    _target_metric_shape,
)
from meta_research.bundle_target_contract import measurement_contract_to_dict
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_implementation_bundle import (
    IMPLEMENTATION_ENTRY_MAX_BYTES, IMPLEMENTATION_ENTRY_MAX_COUNT,
    IMPLEMENTATION_TOTAL_MAX_BYTES, IMPLEMENTATION_BUNDLE_MAX_BYTES,
)

def validate_target_result_tree(value: object, *, depth: int = 0) -> int:
    """Validate one research tree and bound canonical bytes before encoding it.

    Return the exact canonical byte count, permitting the caller to reuse this
    traversal. Shared in-memory subtrees count in full without being expanded.
    """
    from meta_research.target_run_finalizer import TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES

    failures: list[dict[str, object]] = []
    shape = _target_metric_shape(value, starting_depth=depth, failures=failures)
    if shape is None:
        detail = failures[0]
        capacity = detail["rule"] in {"scientific_depth", "json_encoding_capacity"}
        raise OwnerConflict(
            "target_root_result_document_too_large" if capacity else "target_root_result_document_invalid",
            {"category": "technical_capacity" if capacity else "declaration_error", **detail},
        )
    if shape[1] > TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES:
        raise OwnerConflict("target_root_result_document_too_large", {
            "category": "technical_capacity", "path": "$", "rule": "canonical_bytes",
            "actual_bytes": shape[1], "limit": TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES,
        })
    return shape[1]


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
    protocol = authority.measurement_contract.protocol_version
    draft_check_protocol = {
        "protocol_version_ref": authority.identities.protocol_version_ref,
        "required_metric_keys": list(protocol.required_metric_keys),
        "optional_metric_keys": list(protocol.optional_metric_keys),
    }
    return {
        "authority_ref": authority.authority_ref,
        "result_draft_check": {
            "argv": [
                str(Path(sys.executable).absolute()), "-m", "meta_research.target_execution_contract",
                "--check-result", canonical_json(draft_check_protocol),
            ],
            "usage": (
                "形成草稿后，可从本 Target 工作目录按此 argv 运行一次只读指标键检查。"
                "按原参数使用当前冻结 Protocol，在输出中查看缺少或多出的正式评价指标键和草稿摘要。"
                "有确定冲突时修正对应声明并保留已有工作。正式接纳仍由 Owner 在结束后核验。"
            ),
        },
        "authority_hash": authority.authority_hash,
        "target_spec_hash": authority.target_spec_hash,
        "measurement_contract": measurement_contract_to_dict(authority.measurement_contract),
        "artifact_limits": {
            "artifact_bytes": None,
            "artifact_set_bytes": None,
            "result_document_bytes": TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES,
            "result_canonical_bytes": TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES,
            "scientific_tree_depth": TARGET_METRIC_MAX_DEPTH,
            "storage": "RM streams selected files and directory trees into managed storage; available disk space and actual I/O govern large data and checkpoints.",
            "supported_entries": "regular files and directories, no links",
            "legacy_implementation_bundle": {
                "directory_entry_count": IMPLEMENTATION_ENTRY_MAX_COUNT,
                "directory_file_bytes": IMPLEMENTATION_ENTRY_MAX_BYTES,
                "directory_uncompressed_bytes": IMPLEMENTATION_TOTAL_MAX_BYTES,
                "directory_bundle_bytes": IMPLEMENTATION_BUNDLE_MAX_BYTES,
            },
        },
        "result_encoding": "UTF-8 JSON; source bytes retained, semantic hash normalized; JSON integers retain their exact integer type and value, with no fixed 53/63-bit scientific magnitude limit",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Target result draft against its supplied primary Protocol keys.")
    parser.add_argument("--check-result", required=True, metavar="PROTOCOL_JSON")
    args = parser.parse_args(argv)
    try:
        protocol = json.loads(args.check_result)
    except (ValueError, RecursionError):
        parser.error("--check-result needs the JSON from the Target execution contract")
    if (
        not isinstance(protocol, dict)
        or set(protocol) != {"protocol_version_ref", "required_metric_keys", "optional_metric_keys"}
        or not isinstance(protocol["protocol_version_ref"], str)
        or not protocol["protocol_version_ref"]
        or any(
            not isinstance(protocol[key], list)
            or any(not isinstance(name, str) or not name for name in protocol[key])
            for key in ("required_metric_keys", "optional_metric_keys")
        )
    ):
        parser.error("--check-result needs the exact ProtocolVersion ref and its two key lists")

    from meta_research.formal_entities import check_primary_metric_keys
    from meta_research.target_run_finalizer import read_reserved_target_result_draft

    details: dict[str, object] = {"protocol_version_ref": protocol.pop("protocol_version_ref")}
    try:
        draft = read_reserved_target_result_draft(Path.cwd())
        details["draft_sha256"] = draft.raw_sha256
        check = check_primary_metric_keys(draft.document.as_dict(), **protocol)
        status = "keys_match" if check.conforms else "metric_key_mismatch"
        details.update(
            status="no_completed_assessment" if check.run_only else status,
            run_key=check.run_key,
            attempt_key=check.attempt_key,
            missing_metric_keys=list(check.missing_keys),
            unexpected_metric_keys=list(check.unexpected_keys),
        )
    except OwnerConflict as error:
        details.update(status="draft_unreadable", code=error.code)
        if feedback := getattr(error, "feedback", None):
            details["feedback"] = feedback
    print(canonical_json(details))
    return 0


if __name__ == "__main__":
    sys.exit(main())

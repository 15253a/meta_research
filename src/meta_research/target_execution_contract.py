"""Public, frozen Target input contract and bounded JSON result semantics."""
from __future__ import annotations

from pathlib import Path

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
    path = Path(__file__).with_name("skills") / "target-execution" / "SKILL.md"
    return (path.read_text(encoding="utf-8") +
            f"\n\nSkill source: {path.resolve()}\n"
            "Resolve the reference links above relative to this source directory.")

"""Close a failed legacy creation attempt before another formal generation.

This is failure cleanup, never provider completion recovery. Legacy helper
receipts cannot be promoted into the newer signed process-tree contract.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import text

from meta_research.creation_inputs import CreationAnchor, MaterialSet
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.protected_creation_runtime import ProtectedCreationError, _regular_reader
from meta_research.provider_supervisor import (
    ProviderSupervisorError, read_transport_envelope, read_transport_key_for_operation,
)


_LIMIT = 16 * 1024 * 1024


def _bytes(path: Path, *, host_owned: bool = True, private: bool = True) -> bytes:
    import os

    with _regular_reader(path) as stream:
        details = os.fstat(stream.fileno())
        if details.st_nlink != 1 or details.st_size > _LIMIT or (host_owned and (
            details.st_uid != 0 or details.st_mode & (0o077 if private else 0o022)
        )):
            raise OwnerConflict("protected_creation_cleanup_unverified")
        return stream.read(_LIMIT + 1)


def _exact_legacy_launch(work, directory: Path) -> None:
    """Called with the runtime's exclusive sealed-work lock held."""
    _, key = read_transport_key_for_operation(directory)
    _bytes(directory / "invocation.json", private=False)
    invocation = read_transport_envelope(directory / "invocation.json", key)
    if (invocation.get("job_ref") != work.operation.operation_ref
        or invocation.get("operation_name") != "initialization-understanding"
        or invocation.get("provider_execution_binding") != work.execution_binding
        or invocation.get("workspace_binding") != work.binding.seal()
        or invocation.get("working_directory") != str(work.binding.directory)
        or invocation.get("mcp_scope_binding_hash") != work.operation.binding_hash
        or invocation.get("prompt_hash") != canonical_hash(_bytes(directory / "prompt.txt", private=False).decode("utf-8"))
        or invocation.get("output_schema_hash") != canonical_hash(json.loads(_bytes(directory / "output-schema.json", private=False)))):
        raise OwnerConflict("protected_creation_cleanup_unverified")
    if any((directory / name).exists() for name in ("exit.json", "completed.json", "supervisor-exit.json")):
        raise OwnerConflict("protected_creation_cleanup_unverified")
    expected = hashlib.sha256(_bytes(directory / "last-message.json")).digest()
    matches = []
    control = work.runtime._control
    for path in control.glob("launch-*.json"):
        if path.name.endswith(".receipt.json"):
            continue
        launch = json.loads(_bytes(path))
        if launch.get("jail") != str(work.runtime.jail_root):
            raise OwnerConflict("protected_creation_cleanup_unverified")
        argv = launch.get("argv")
        if not isinstance(argv, list) or argv.count("--output-last-message") != 1:
            continue
        index = argv.index("--output-last-message") + 1
        if index == len(argv) or not isinstance(argv[index], str) or re.fullmatch(
            r"/outputs/[0-9a-f]{16}-0000-last-message\.json", argv[index]
        ) is None:
            raise OwnerConflict("protected_creation_cleanup_unverified")
        output = work.runtime.jail_root / argv[index].lstrip("/")
        if not output.exists():
            continue
        if hashlib.sha256(_bytes(output, host_owned=False)).digest() != expected:
            continue
        receipt_path = path.with_suffix(".receipt.json")
        if launch.get("receipt") != str(receipt_path):
            raise OwnerConflict("protected_creation_cleanup_unverified")
        receipt = json.loads(_bytes(receipt_path))
        if (receipt.get("status") != "completed" or type(receipt.get("returncode")) is not int
            or receipt["returncode"] != 0 or receipt.get("descendants_ended") is not True):
            raise OwnerConflict("protected_creation_cleanup_unverified")
        matches.append(path)
    if len(matches) != 1:
        raise OwnerConflict("protected_creation_cleanup_unverified")


def close_failed_initialization_work(adapter, initialization_id, draft_revision, draft_hash):
    roots = adapter._workspaces
    if roots is None:
        return
    human = roots._hc
    view = human.query_quest_creation(initialization_id)
    if (view["quest_draft"]["revision"], view["quest_draft"]["hash"]) != (draft_revision, draft_hash):
        raise OwnerConflict("creation_input_stale")
    session = view["intent_session"]["ref"]
    binding = roots.bind_initialization(initialization_id, session)
    with human._database.read() as connection:
        rows = connection.execute(text("SELECT operation.* FROM hc_creation_material_operations operation "
            "JOIN hc_proposal_generation_attempts generation ON "
            "operation.operation_ref=generation.generation_ref || ':proposal:understanding' "
            "WHERE operation.root_session_ref=:session AND generation.initialization_id=:initialization "
            "AND generation.status='failed' AND generation.failure_code='proposal_output_invalid' "
            "AND operation.state IN ('active','unknown_outcome')"),
            {"session": session, "initialization": initialization_id}).all()
    for row in rows:
        anchor = json.loads(row.anchor_json)
        if anchor.get("kind") != "quest_initialization" or anchor.get("ref") != initialization_id:
            raise OwnerConflict("protected_creation_cleanup_unverified")
        suffix = ":proposal:understanding"
        generation_ref = row.operation_ref[:-len(suffix)]
        with human._database.read() as connection:
            generation = connection.execute(text("SELECT * FROM hc_proposal_generation_attempts WHERE generation_ref=:ref"),
                {"ref": generation_ref}).first()
        if (generation is None or generation.initialization_id != initialization_id
            or generation.status != "failed" or generation.failure_code != "proposal_output_invalid"
            or row.state != "active" or row.identity_json is not None
            or (generation.basis_revision, generation.basis_hash) != (draft_revision, draft_hash)
            or anchor != CreationAnchor("quest_initialization", initialization_id, None, draft_revision, draft_hash).as_dict()):
            raise OwnerConflict("protected_creation_cleanup_unverified")
        material_set = json.loads(row.material_set_json)
        inputs = MaterialSet(CreationAnchor(**anchor), tuple(material_set["references"]))
        if inputs.as_dict() != material_set or inputs.set_hash != row.material_set_hash:
            raise OwnerConflict("protected_creation_cleanup_unverified")
        previous = getattr(getattr(human, "_creation_material_operations", {}).get(row.operation_ref), "work", None)
        if previous is not None and (previous.operation.fence_ref != row.fence_ref
            or previous.operation.binding_hash != row.binding_hash):
            raise OwnerConflict("protected_creation_cleanup_unverified")
        try:
            work = roots.protected_creation(binding, inputs, operation_ref=row.operation_ref)
            if work.operation.fence_ref != row.fence_ref or work.operation.binding_hash != row.binding_hash:
                raise OwnerConflict("protected_creation_cleanup_unverified")
            directory = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": row.operation_ref}) / "initialization-understanding"
            with work.runtime.exclusive_sealed_work():
                _exact_legacy_launch(work, directory)
                # Recheck the business authority after filesystem verification.
                with human._database.read() as connection:
                    status = connection.execute(text("SELECT status FROM hc_proposal_generation_attempts WHERE generation_ref=:ref"),
                        {"ref": generation_ref}).scalar_one()
                if status != "failed":
                    raise OwnerConflict("protected_creation_cleanup_unverified")
                work.fail()
                if previous is not None and previous is not work:
                    previous.fail()
        except (OSError, ValueError, TypeError, KeyError, ProtectedCreationError, ProviderSupervisorError) as error:
            raise OwnerConflict("protected_creation_cleanup_unverified") from error

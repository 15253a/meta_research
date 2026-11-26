"""Close a failed creation attempt before another formal generation.

This is failure cleanup, never provider completion recovery. Host-direct
creation operations reconcile their durable supervisor spool and revoke active
material operations without touching the legacy chroot runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from meta_research.creation_inputs import CreationAnchor, MaterialSet
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError, read_transport_envelope, read_transport_key_for_operation,
)


_LIMIT = 16 * 1024 * 1024


def _read_regular_bytes(path: Path) -> bytes:
    import os
    import stat

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > _LIMIT:
            raise OwnerConflict("protected_creation_cleanup_unverified")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read(_LIMIT + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_host_failed_operation(adapter, work, directory: Path) -> None:
    if not (directory / "invocation.json").is_file():
        raise OwnerConflict("protected_creation_cleanup_unverified")
    _, key = read_transport_key_for_operation(directory)
    invocation = read_transport_envelope(directory / "invocation.json", key)
    if (invocation.get("job_ref") != work.operation.operation_ref
        or invocation.get("operation_name") != "initialization-understanding"
        or invocation.get("provider_execution_binding") != work.execution_binding
        or invocation.get("workspace_binding") != work.binding.seal()
        or invocation.get("working_directory") != str(work.binding.directory)
        or invocation.get("mcp_scope_binding_hash") != work.operation.binding_hash
        or invocation.get("prompt_hash") != canonical_hash(_read_regular_bytes(directory / "prompt.txt").decode("utf-8"))
        or invocation.get("output_schema_hash") != canonical_hash(json.loads(_read_regular_bytes(directory / "output-schema.json")))):
        raise OwnerConflict("protected_creation_cleanup_unverified")
    if (directory / "completed.json").exists():
        raise OwnerConflict("protected_creation_cleanup_unverified")
    reconcile = getattr(adapter, "_reconcile_cancelled_job_spools", None)
    if callable(reconcile):
        if not reconcile(work.operation.operation_ref):
            raise OwnerConflict("protected_creation_cleanup_unverified")
    elif not adapter.reconcile_cancelled_job(work.operation.operation_ref):
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
            _verify_host_failed_operation(adapter, work, directory)
            # Recheck the business authority after filesystem verification.
            with human._database.read() as connection:
                status = connection.execute(text("SELECT status FROM hc_proposal_generation_attempts WHERE generation_ref=:ref"),
                    {"ref": generation_ref}).scalar_one()
            if status != "failed":
                raise OwnerConflict("protected_creation_cleanup_unverified")
            work.fail()
            if previous is not None and previous is not work:
                previous.fail()
        except (OSError, ValueError, TypeError, KeyError, ProviderSupervisorError, IdeaSkillUnavailable) as error:
            raise OwnerConflict("protected_creation_cleanup_unverified") from error

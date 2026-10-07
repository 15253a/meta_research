from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from meta_research.creation_basis import empty_manifest
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.root_workspace import _read_bytes


def deliver_materials(owner, initialization_id, payload, idempotency_key):
    creation = owner.query_quest_creation(initialization_id)
    draft_key = creation["quest_draft"]
    if creation["status"] in {"confirmed", "completed", "cancelled"}:
        raise OwnerConflict("quest_initialization_is_terminal")
    root_ref = creation["intent_session"]["ref"]
    submission = payload["submission_ref"]
    destination = owner._creation_workspaces.destination_for_initialization(initialization_id, root_ref)
    draft = dict(draft_key["value"])
    manifest = dict(draft.get("material_manifest", empty_manifest()))
    entries = {item["material_key"]: item for item in manifest["entries"]}
    submissions = {item["submission_ref"]: item for item in manifest["submissions"]}
    request_hash = canonical_hash({key: value for key, value in payload.items() if not key.startswith("expected_")})
    existing = submissions.get(submission)
    if existing is not None and any(batch["idempotency_key"] == idempotency_key and batch["request_hash"] == request_hash for batch in existing["batches"]):
        return creation
    if (draft_key["hash"], draft_key["revision"]) != (payload["expected_draft_hash"], payload["expected_draft_revision"]):
        raise OwnerConflict("quest_draft_stale")
    if existing is not None and (existing["complete"] or any(batch["idempotency_key"] == idempotency_key for batch in existing["batches"])):
        raise OwnerConflict("creation_material_delivery_conflict")
    origin_kind = "browser_folder" if payload.get("folder") else "browser_file"
    if payload.get("locator") is not None:
        origin_kind = "server_path"
        root = Path(payload["locator"])
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise OwnerConflict("creation_material_path_invalid")
        files = []
        for current, directories, names in os.walk(root, followlinks=False):
            if any((Path(current) / name).is_symlink() for name in (*directories, *names)):
                raise OwnerConflict("workspace_file_unsafe")
            for name in sorted(names):
                relative = (Path(current) / name).relative_to(root).as_posix()
                files.append((relative, _read_bytes(root, relative)))
                if len(files) > 10000:
                    raise OwnerConflict("creation_material_count_exceeded")
    else:
        try:
            files = [(item["relative_path"], base64.b64decode(item["content_base64"], validate=True)) for item in payload["files"]]
        except (binascii.Error, ValueError) as error:
            raise OwnerConflict("creation_material_content_invalid") from error
    if sum(len(content) for _, content in files) > 64 * 1024 * 1024:
        raise OwnerConflict("creation_material_size_exceeded")
    if not files:
        raise OwnerConflict("creation_material_delivery_empty")
    batch_records = []
    for start in range(0, len(files), 100):
        delivery_ref = "creation-material:" + canonical_hash({"submission": submission, "batch": request_hash, "start": start})
        delivered = owner._creation_workspaces.deliver(destination, delivery_ref=delivery_ref, files=tuple(files[start:start + 100]))
        for receipt, (relative, _) in zip(sorted(delivered["files"], key=lambda item: item["path"]), sorted(files[start:start + 100])):
            origin = {"kind": origin_kind, "submission_ref": submission, "relative_path": relative,
                "locator": payload.get("locator")}
            key = canonical_hash({"initialization_id": initialization_id, "origin": origin, "sha256": receipt["sha256"]})
            if any(item["origin"]["submission_ref"] == submission and item["relative_path"] == relative and item["material_key"] != key for item in entries.values()):
                raise OwnerConflict("creation_material_delivery_conflict")
            entries[key] = {**receipt, "workspace_ref": delivered["workspace_ref"], "relative_path": relative,
                "material_key": key, "origin": origin}
        batch_records.append(delivery_ref)
    prior = [] if existing is None else existing["batches"]
    submissions[submission] = {"submission_ref": submission, "complete": payload.get("complete", True),
        "batches": sorted([*prior, {"idempotency_key": idempotency_key, "request_hash": request_hash, "deliveries": batch_records}], key=lambda item: item["request_hash"])}
    draft["material_manifest"] = {"entries": sorted(entries.values(), key=lambda item: item["material_key"]),
        "submissions": sorted(submissions.values(), key=lambda item: item["submission_ref"])}
    return owner.revise_quest_draft(initialization_id, draft, draft_key["hash"], "material-draft:" + canonical_hash({"key": idempotency_key}),
        draft_key["revision"], material_delivery=True)

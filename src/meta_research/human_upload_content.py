"""Exact uploaded human materials retained in their accepting HC commands."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash


def _document(value, code):
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as error:
        raise OwnerConflict(code) from error
    if not isinstance(result, dict):
        raise OwnerConflict(code)
    return result


def _guidance_uploads(human, connection, quest_ref, source_ref, version_ref):
    from meta_research.owners.human_collaboration_ladder import guidance_binding_from_row

    row = connection.execute(text(
        "SELECT * FROM hc_soft_constraints WHERE constraint_ref=:ref AND scope_ref=:scope"),
        {"ref": source_ref, "scope": "quest:" + quest_ref}).first()
    if row is None:
        raise OwnerConflict("content_source_unbound")
    binding = guidance_binding_from_row(row)
    if binding["guidance_hash"] != version_ref:
        raise OwnerConflict("content_version_unbound")
    rows = connection.execute(text(
        "SELECT s.command_json,s.command_hash,s.receiver_json,s.idempotency_key,"
        "c.command_kind,c.request_hash,c.result_ref FROM hc_work_material_submissions s "
        "LEFT JOIN hc_collaboration_commands c ON c.idempotency_key=s.idempotency_key "
        "WHERE s.anchor_kind='guidance' AND s.anchor_ref=:ref AND s.state='ready' "
        "ORDER BY s.created_at,s.submission_ref"), {"ref": source_ref}).all()
    uploads = []
    for saved in rows:
        command = _document(saved.command_json, "material_receipt_invalid")
        receiver = _document(saved.receiver_json, "material_receipt_invalid")
        if (canonical_hash(command) != saved.command_hash or command.get("receiver") != receiver
                or receiver.get("quest_ref") != quest_ref
                or saved.idempotency_key != row.idempotency_key or saved.result_ref != source_ref):
            raise OwnerConflict("material_receipt_invalid")
        if row.source_proposal_ref is None:
            expected = canonical_hash({"scope_ref": row.scope_ref, "guidance": binding["guidance"],
                "work_materials": command})
            if saved.command_kind != "guidance_submit" or saved.request_hash != expected:
                raise OwnerConflict("material_receipt_invalid")
        elif (saved.command_kind != "proposal_to_constraint"
                or binding["guidance"].get("work_materials") != command):
            # Converted guidance already binds the whole confirmed material
            # command in the verified guidance hash, including its uploads.
            raise OwnerConflict("material_receipt_invalid")
        uploads.extend(command.get("uploads", []))
    return uploads, "material_receipt_invalid"


def _response_uploads(human, connection, quest_ref, source_ref, version_ref):
    response_row = connection.execute(text(
        "SELECT request_ref,command_hash FROM hc_human_request_responses WHERE response_ref=:ref"),
        {"ref": source_ref}).first()
    if response_row is None:
        raise OwnerConflict("content_source_unbound")
    selected = human.query_research_help_response(response_row.request_ref, source_ref, quest_ref=quest_ref)
    response = selected["response"]
    if canonical_hash(response) != version_ref:
        raise OwnerConflict("content_version_unbound")
    if "delivery" not in response:
        return [], "human_response_spool_invalid"
    receipt = human._fact_verifier.verify_reply_delivery(request_ref=response_row.request_ref, response_ref=source_ref)
    row = connection.execute(text(
        "SELECT * FROM hc_reply_deliveries WHERE request_ref=:request AND response_ref=:response AND state='ready'"),
        {"request": response_row.request_ref, "response": source_ref}).first()
    if row is None:
        raise OwnerConflict("human_response_delivery_unverified")
    spool = _document(row.command_json, "human_response_spool_invalid")
    command = spool.get("reply")
    destination = _document(row.destination_json, "human_response_spool_invalid")
    if (not isinstance(command, dict) or canonical_hash(command) != row.command_hash
            or response_row.command_hash != row.command_hash
            or canonical_hash({"command_hash": row.command_hash, "destination": destination}) != row.manifest_hash
            or receipt.get("delivery_ref") != row.delivery_ref
            or destination.get("quest_ref") != quest_ref
            or destination.get("human_request_ref") != response_row.request_ref):
        raise OwnerConflict("human_response_spool_invalid")
    uploads = [item for item in command.get("materials", []) if item.get("kind") == "upload"]
    published = receipt.get("uploaded_files", [])
    expected = [{"workspace_ref": destination["workspace_ref"],
        "path": "inbox/" + canonical_hash({"delivery_ref": row.delivery_ref}) + "/" + item["relative_path"],
        "sha256": item["sha256"]} for item in uploads]
    if published != expected:
        raise OwnerConflict("human_response_delivery_unverified")
    return uploads, "human_response_spool_invalid"


def _uploads(human, *, quest_ref, source_ref, version_ref):
    with human._database.read_snapshot() as connection:
        if source_ref.startswith("soft_constraint_"):
            uploads, code = _guidance_uploads(human, connection, quest_ref, source_ref, version_ref)
        else:
            uploads, code = _response_uploads(human, connection, quest_ref, source_ref, version_ref)
    paths = set()
    for item in uploads:
        if (not isinstance(item, dict) or not isinstance(item.get("relative_path"), str)
                or not item["relative_path"] or item["relative_path"] in paths
                or not isinstance(item.get("media_type"), str)
                or not isinstance(item.get("content_base64"), str)):
            raise OwnerConflict(code)
        paths.add(item["relative_path"])
    return uploads, code


def _bytes(upload, code):
    try:
        content = base64.b64decode(upload["content_base64"], validate=True)
    except (ValueError, binascii.Error) as error:
        raise OwnerConflict(code) from error
    digest = hashlib.sha256(content).hexdigest()
    if upload.get("sha256", digest) != digest:
        raise OwnerConflict(code)
    return content, digest


def human_upload_readers(human, *, quest_ref, source_ref, version_ref, source_quest_ref=None):
    """Describe only exact retained uploads; no workspace authority is granted."""
    uploads, code = _uploads(human, quest_ref=quest_ref, source_ref=source_ref, version_ref=version_ref)
    result = []
    for upload in uploads:
        content, digest = _bytes(upload, code)
        args = {"source_ref": source_ref, "version_ref": version_ref, "entry_path": upload["relative_path"]}
        if source_quest_ref is not None:
            args["source_quest_ref"] = source_quest_ref
        result.append({"path": upload["relative_path"], "media_type": upload["media_type"],
            "content_hash": digest, "bytes": len(content),
            "reader": {"operation": "research_memory.content.read", "args": args}})
    return result


def read_human_upload(human, *, quest_ref, source_ref, version_ref, entry_path, offset=0, limit=8192):
    """Page one selected upload from the verified immutable submission bytes."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
        raise OwnerConflict("content_page_invalid")
    uploads, code = _uploads(human, quest_ref=quest_ref, source_ref=source_ref, version_ref=version_ref)
    selected = [item for item in uploads if item["relative_path"] == entry_path]
    if len(selected) != 1:
        raise OwnerConflict("content_entry_invalid")
    upload = selected[0]
    content, digest = _bytes(upload, code)
    if offset > len(content):
        raise OwnerConflict("content_page_invalid")
    data = content[offset:offset + limit]
    encoding = "utf-8"
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data" and error.start > 0:
            data = data[:error.start]
            body = data.decode("utf-8")
        else:
            encoding = "base64"
            body = base64.b64encode(data).decode("ascii")
    next_offset = offset + len(data)
    return {"source_ref": source_ref, "version_ref": version_ref, "entry_path": entry_path,
        "media_type": upload["media_type"], "content_hash": digest, "entry_hash": digest,
        "encoding": encoding, "text": body, "offset": offset, "offset_unit": "bytes",
        "returned_bytes": len(data), "total_bytes": len(content), "next_offset": next_offset,
        "complete": next_offset == len(content)}

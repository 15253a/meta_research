"""Editable Quest conditions, read for new model operations without changing facts."""

from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading

from meta_research.owners.common import OwnerConflict, canonical_hash


_LOCK = threading.RLock()
_PREFIX = "[meta-research runtime conditions v1; characters="
_LIMIT = 24000
_INIT_PREFIX = "initialization:"


def _data_root(workspace: Path) -> Path | None:
    current = Path(workspace).resolve()
    return next((path for path in (current, *current.parents)
                 if (path / "data-root.json").is_file()), None)


def _initial_conditions(root: Path, *, quest_ref=None, run_ref=None,
                        initialization_id=None, target_ref=None):
    database = root / "meta-research.sqlite3"
    if not database.is_file():
        return None
    def agree(left, right):
        if left is not None and right is not None and left != right:
            raise OwnerConflict("runtime_conditions_scope_conflict")
        return left if left is not None else right

    if isinstance(quest_ref, str) and quest_ref.startswith(_INIT_PREFIX):
        initialization_id = agree(initialization_id, quest_ref[len(_INIT_PREFIX):])
        quest_ref = None
    for ref in (quest_ref, run_ref, initialization_id, target_ref):
        if ref is not None and (not isinstance(ref, str) or not ref or len(ref) > 160):
            raise OwnerConflict("runtime_conditions_scope_invalid")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if run_ref is not None:
            row = db.execute("SELECT quest_ref FROM ar_run_controls WHERE run_ref=?", (run_ref,)).fetchone()
            if row is not None:
                quest_ref = agree(quest_ref, row["quest_ref"])
            # Target Harness runs need not have an ar_run_controls row. Read
            # their frozen request identity, never infer scope from the worker.
            harness = db.execute("SELECT request_json,request_hash FROM ar_harness_runs WHERE run_ref=?",
                                 (run_ref,)).fetchone()
            if harness is not None:
                try:
                    request = json.loads(harness["request_json"])
                except (ValueError, TypeError) as error:
                    raise OwnerConflict("runtime_conditions_run_invalid") from error
                if (not isinstance(request, dict)
                        or canonical_hash(request) != harness["request_hash"]):
                    raise OwnerConflict("runtime_conditions_run_invalid")
                if "target_ref" in request or "target_run_ref" in request:
                    if (request.get("target_run_ref") != run_ref
                            or not isinstance(request.get("target_ref"), str)
                            or not request["target_ref"]):
                        raise OwnerConflict("runtime_conditions_run_invalid")
                    target_ref = agree(target_ref, request["target_ref"])
            # Initial DeepFetch has no accepted Quest yet. Later DeepFetch uses
            # the manual request table; neither may inherit the foreground Quest.
            deepfetch = db.execute(
                "SELECT d.initialization_id,NULL AS quest_ref FROM ar_deepfetch_runs r "
                "JOIN hc_deepfetch_requests d ON d.request_ref=r.request_ref WHERE r.run_ref=? "
                "UNION ALL SELECT d.initialization_id,d.quest_ref FROM ar_deepfetch_runs r "
                "JOIN hc_manual_deepfetch_requests d ON d.request_ref=r.request_ref WHERE r.run_ref=?",
                (run_ref, run_ref),
            ).fetchall()
            if row is None and harness is None and not deepfetch:
                raise OwnerConflict("runtime_conditions_run_not_found")
            for item in deepfetch:
                initialization_id = agree(initialization_id, item["initialization_id"])
                quest_ref = agree(quest_ref, item["quest_ref"])
        if target_ref is not None:
            row = db.execute("SELECT g.quest_ref FROM rg_targets t JOIN rg_target_graphs g "
                             "ON g.graph_ref=t.graph_ref WHERE t.target_ref=?", (target_ref,)).fetchone()
            if row is None:
                raise OwnerConflict("runtime_conditions_target_not_found")
            quest_ref = agree(quest_ref, row["quest_ref"])
        if initialization_id is not None:
            row = db.execute("SELECT quest_ref FROM rg_quests WHERE initialization_id=?",
                             (initialization_id,)).fetchone()
            if row is not None:
                quest_ref = agree(quest_ref, row["quest_ref"])
        if quest_ref is not None:
            row = db.execute("SELECT initialization_id,goal_json,draft_hash FROM rg_quests WHERE quest_ref=?", (quest_ref,)).fetchone()
            if row is None:
                raise OwnerConflict("runtime_conditions_quest_not_found")
            initialization_id = agree(initialization_id, row["initialization_id"])
            encoded, draft_hash = row["goal_json"], row["draft_hash"]
        elif initialization_id is not None:
            row = db.execute("SELECT draft_json,draft_hash FROM hc_quest_initializations WHERE initialization_id=?", (initialization_id,)).fetchone()
            if row is None:
                raise OwnerConflict("runtime_conditions_initialization_not_found")
            encoded, draft_hash = row["draft_json"], row["draft_hash"]
        else:
            return None
        try:
            draft = json.loads(encoded)
        except (ValueError, TypeError) as error:
            raise OwnerConflict("runtime_conditions_draft_invalid") from error
        if not isinstance(draft, dict) or canonical_hash(draft) != draft_hash:
            raise OwnerConflict("runtime_conditions_draft_invalid")
        envelope = None
        if bool(draft.get("resource_envelope_ref")) != bool(draft.get("resource_envelope_hash")):
            raise OwnerConflict("runtime_conditions_resource_invalid")
        if draft.get("resource_envelope_ref"):
            row = db.execute("SELECT envelope_json,envelope_hash FROM hc_resource_envelopes "
                             "WHERE envelope_ref=? AND initialization_id=?",
                             (draft["resource_envelope_ref"], initialization_id)).fetchone()
            if row is None or row["envelope_hash"] != draft.get("resource_envelope_hash"):
                raise OwnerConflict("runtime_conditions_resource_invalid")
            try:
                envelope = json.loads(row["envelope_json"])
            except (ValueError, TypeError) as error:
                raise OwnerConflict("runtime_conditions_resource_invalid") from error
            if not isinstance(envelope, dict) or canonical_hash(envelope) != row["envelope_hash"]:
                raise OwnerConflict("runtime_conditions_resource_invalid")
    values = {key: draft[key] for key in ("time_budget", "key_configuration")
              if draft.get(key) is not None}
    literature = draft.get("literature")
    if isinstance(literature, dict):
        values["literature"] = {key: literature[key] for key in ("mode", "scope_exclusions")
                                if isinstance(literature.get(key), str)}
    if envelope is not None:
        devices, uuids = envelope.get("selected_devices"), envelope.get("selected_device_uuids")
        if (not isinstance(devices, list) or not isinstance(uuids, list)
                or any(not isinstance(uuid, str) or not uuid for uuid in uuids)
                or len(uuids) != len(set(uuids))
                or any(not isinstance(device, dict) for device in devices)
                or [device.get("uuid") for device in devices] != uuids
                or envelope.get("time_budget") != draft.get("time_budget")):
            raise OwnerConflict("runtime_conditions_resource_invalid")
        values["selected_devices"] = [
            {key: device[key] for key in ("uuid", "name", "memory_total_mib") if key in device}
            for device in devices
        ]
        values["selected_device_uuids"] = uuids
    if not values.get("selected_device_uuids"):
        values["gpu_configuration"] = "未配置已选 GPU；不得将机器探测到的其他显卡当作用户已选择的资源。"
    initial_text = (
        "按以下条件安排研究与委派；显卡是用户选定的可用资源，按工作需要使用，无需为了使用显卡而改变方法。"
        "时间预算是整个 Quest 的预算，进入新 Cycle 不会重置。\n"
        + json.dumps(values, ensure_ascii=False, indent=2)
    )
    scope = quest_ref or _INIT_PREFIX + initialization_id
    # An accepted Quest inherits edits made during its own initialization. A
    # Quest-specific save then takes precedence without changing accepted facts.
    if quest_ref is not None:
        initial_text = _read(root, _INIT_PREFIX + initialization_id, initial_text)["text"]
    return scope, initial_text


def _value(quest_ref: str, text: str) -> dict[str, str]:
    return {"quest_ref": quest_ref, "text": text,
            "revision": canonical_hash({"quest_ref": quest_ref, "text": text})}


def _read(root: Path, quest_ref: str, default_text: str) -> dict[str, str]:
    path = root / "runtime-conditions" / (canonical_hash(quest_ref) + ".json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _value(quest_ref, default_text)
    except (ValueError, UnicodeError) as error:
        raise OwnerConflict("runtime_conditions_invalid") from error
    if (not isinstance(saved, dict) or saved.get("quest_ref") != quest_ref
            or not isinstance(saved.get("text"), str) or len(saved["text"]) > _LIMIT
            or saved != _value(quest_ref, saved["text"])):
        raise OwnerConflict("runtime_conditions_invalid")
    return saved


def read_runtime_conditions(workspace: Path, quest_ref: str) -> dict[str, str]:
    root = _data_root(workspace)
    if root is None:
        raise OwnerConflict("runtime_conditions_unavailable")
    initial = _initial_conditions(root, quest_ref=quest_ref)
    if initial is None:
        raise OwnerConflict("runtime_conditions_quest_not_found")
    return _read(root, initial[0], initial[1])


def save_runtime_conditions(workspace: Path, quest_ref: str, *, text: str,
                            expected_revision: str) -> dict[str, str]:
    if not isinstance(text, str) or not text.strip() or len(text) > _LIMIT:
        raise OwnerConflict("runtime_conditions_text_invalid")
    with _LOCK:
        current = read_runtime_conditions(workspace, quest_ref)
        if expected_revision != current["revision"]:
            raise OwnerConflict("runtime_conditions_stale")
        root = _data_root(workspace)
        assert root is not None
        directory = root / "runtime-conditions"
        directory.mkdir(parents=True, exist_ok=True)
        scope = current["quest_ref"]
        path = directory / (canonical_hash(scope) + ".json")
        saved = _value(scope, text)
        fd, name = tempfile.mkstemp(prefix=".conditions-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(saved, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return saved


def render_runtime_conditions(workspace: Path, *, quest_ref=None, run_ref=None,
                              initialization_id=None, target_ref=None) -> str:
    root = _data_root(workspace)
    if root is None:
        return ""
    initial = _initial_conditions(root, quest_ref=quest_ref, run_ref=run_ref,
                                  initialization_id=initialization_id, target_ref=target_ref)
    if initial is None:
        return ""
    scope, default_text = initial
    current = _read(root, scope, default_text)
    return (
        "本次调用的当前运行条件（由系统读取用户配置）：遵循下列最新条件，并传给委派的智能体。"
        "同类旧运行条件以本段为准；已经接纳的研究成果、精确输入与正式授权仍按原记录核验。\n"
        + json.dumps(current, ensure_ascii=False, separators=(",", ":"))
    )


def compose_runtime_prompt(prompt: str, conditions: str) -> str:
    if not conditions:
        return prompt
    return f"{_PREFIX}{len(conditions)}]\n{conditions}\n\n{prompt}"


def split_runtime_prompt(prompt: str) -> tuple[str, str]:
    if not prompt.startswith(_PREFIX):
        return "", prompt
    header, separator, body = prompt.partition("\n")
    length_text = header[len(_PREFIX):-1]
    if not separator or not header.endswith("]") or not length_text.isdecimal():
        raise ValueError("runtime_conditions_prompt_invalid")
    length = int(length_text)
    if length < 1 or body[length:length + 2] != "\n\n":
        raise ValueError("runtime_conditions_prompt_invalid")
    return body[:length], body[length + 2:]

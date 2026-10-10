"""Editable Quest conditions, read for new model operations without changing facts."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

from sqlalchemy import text as sql_text

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.research_style import validate_research_style, render_research_style


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
    style = validate_research_style(draft.get('research_style', 'balanced'))
    # An accepted Quest inherits edits made during its own initialization. A
    # Quest-specific save then takes precedence without changing accepted facts.
    if quest_ref is not None:
        inherited = _read(root, _INIT_PREFIX + initialization_id, initial_text, style)
        initial_text, style = inherited['text'], inherited['research_style']
    configuration = {"mode": literature.get("mode", "oa_only"),
                     "library_entry_url": literature.get("library_entry_url", ""),
                     "institution_required": literature.get("institution_required", False)} if isinstance(literature, dict) else None
    if configuration is not None:
        try:
            configuration = _literature_configuration(configuration)
        except OwnerConflict:
            # Historical conditions remain readable; invalid private connection
            # data is never promoted to a newly editable configuration.
            configuration = None
    return scope, initial_text, style, configuration


def _literature_configuration(value):
    from meta_research.owners.human_collaboration import _validated_library_entry_url
    if (not isinstance(value, dict) or set(value) != {"mode", "library_entry_url", "institution_required"}
            or value["mode"] not in {"oa_then_institution", "oa_only", "provided_only"}
            or not isinstance(value["library_entry_url"], str)
            or len(value["library_entry_url"]) > 4000
            or type(value["institution_required"]) is not bool
            or value["institution_required"] and value["mode"] != "oa_then_institution"):
        raise OwnerConflict("literature_configuration_invalid")
    return {**value, "library_entry_url": _validated_library_entry_url(value["library_entry_url"])}


def _value(quest_ref: str, text: str, research_style: str = 'balanced', literature_configuration=None) -> dict[str, object]:
    fields = {'quest_ref': quest_ref, 'text': text,
              'research_style': validate_research_style(research_style)}
    if literature_configuration is not None:
        fields["literature_configuration"] = _literature_configuration(literature_configuration)
    return {**fields, 'revision': canonical_hash(fields)}


def _legacy_value(
    root: Path, quest_ref: str, default_text: str, default_style: str
) -> tuple[dict[str, str], str]:
    path = root / "runtime-conditions" / (canonical_hash(quest_ref) + ".json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _value(quest_ref, default_text, default_style), "initial_default"
    except (ValueError, UnicodeError) as error:
        raise OwnerConflict("runtime_conditions_invalid") from error
    if (not isinstance(saved, dict) or saved.get("quest_ref") != quest_ref
            or not isinstance(saved.get("text"), str) or len(saved["text"]) > _LIMIT):
        raise OwnerConflict("runtime_conditions_invalid")
    if 'research_style' not in saved:
        legacy = {'quest_ref': quest_ref, 'text': saved['text']}
        if saved != {**legacy, 'revision': canonical_hash(legacy)}:
            raise OwnerConflict('runtime_conditions_invalid')
        return _value(quest_ref, saved['text'], default_style), "legacy_file"
    if saved != _value(quest_ref, saved['text'], saved['research_style']):
        raise OwnerConflict('runtime_conditions_invalid')
    return saved, "legacy_file"


def _stored_value(scope: str, encoded: str, revision: str) -> dict[str, str]:
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("runtime_conditions_invalid") from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("text"), str)
        or not value["text"].strip()
        or len(value["text"]) > _LIMIT
        or not isinstance(value.get("research_style"), str)
    ):
        raise OwnerConflict("runtime_conditions_invalid")
    try:
        expected = _value(scope, value["text"], value["research_style"], value.get("literature_configuration"))
    except (TypeError, ValueError) as error:
        raise OwnerConflict("runtime_conditions_invalid") from error
    if value != expected or value.get("revision") != revision:
        raise OwnerConflict("runtime_conditions_invalid")
    return expected


def _read(
    root: Path, quest_ref: str, default_text: str, default_style: str = "balanced", default_literature=None
) -> dict[str, str]:
    def with_library(value):
        # Existing stored revisions remain valid. Their fallback is the immutable
        # accepted Quest draft; the next explicit save versions the configuration.
        if "literature_configuration" not in value and default_literature is not None:
            return {**value, "literature_configuration": default_literature}
        return value
    database = root / "meta-research.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT v.value_json,v.revision FROM hc_runtime_condition_heads h "
            "JOIN hc_runtime_condition_versions v ON v.scope_ref=h.scope_ref "
            "AND v.revision=h.current_revision WHERE h.scope_ref=?",
            (quest_ref,),
        ).fetchone()
        if row is not None:
            return with_library(_stored_value(quest_ref, row["value_json"], row["revision"]))
    imported, source_kind = _legacy_value(
        root, quest_ref, default_text, default_style
    )
    with closing(sqlite3.connect(database, timeout=5.0)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT v.value_json,v.revision FROM hc_runtime_condition_heads h "
            "JOIN hc_runtime_condition_versions v ON v.scope_ref=h.scope_ref "
            "AND v.revision=h.current_revision WHERE h.scope_ref=?",
            (quest_ref,),
        ).fetchone()
        if row is None:
            now = time.time()
            db.execute(
                "INSERT INTO hc_runtime_condition_versions "
                "(scope_ref,revision,value_json,source_kind,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    quest_ref,
                    imported["revision"],
                    json.dumps(
                        imported,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    source_kind,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO hc_runtime_condition_heads "
                "(scope_ref,current_revision,updated_at) VALUES (?,?,?)",
                (quest_ref, imported["revision"], now),
            )
            db.commit()
            return with_library(imported)
        db.commit()
        return with_library(_stored_value(quest_ref, row["value_json"], row["revision"]))


def read_runtime_conditions_in_transaction(
    connection, quest_ref: str, *, revision: str | None = None
) -> dict[str, str]:
    if revision is not None:
        row = connection.execute(
            sql_text(
                "SELECT value_json,revision FROM hc_runtime_condition_versions "
                "WHERE scope_ref=:scope_ref AND revision=:revision"
            ),
            {"scope_ref": quest_ref, "revision": revision},
        ).first()
        if row is None:
            raise OwnerConflict("runtime_conditions_revision_not_found")
        return _stored_value(quest_ref, row.value_json, row.revision)
    row = connection.execute(
        sql_text(
            "SELECT v.value_json,v.revision FROM hc_runtime_condition_heads h "
            "JOIN hc_runtime_condition_versions v ON v.scope_ref=h.scope_ref "
            "AND v.revision=h.current_revision WHERE h.scope_ref=:scope_ref"
        ),
        {"scope_ref": quest_ref},
    ).first()
    if row is None:
        raise OwnerConflict("runtime_conditions_unseeded")
    return _stored_value(quest_ref, row.value_json, row.revision)


def read_runtime_conditions(workspace: Path, quest_ref: str) -> dict[str, str]:
    root = _data_root(workspace)
    if root is None:
        raise OwnerConflict("runtime_conditions_unavailable")
    initial = _initial_conditions(root, quest_ref=quest_ref)
    if initial is None:
        raise OwnerConflict("runtime_conditions_quest_not_found")
    return _read(root, *initial)


def save_runtime_conditions(workspace: Path, quest_ref: str, *, text: str,
                            expected_revision: str, research_style: str | None = None,
                            literature_configuration=None) -> dict[str, str]:
    if not isinstance(text, str) or not text.strip() or len(text) > _LIMIT:
        raise OwnerConflict("runtime_conditions_text_invalid")
    with _LOCK:
        current = read_runtime_conditions(workspace, quest_ref)
        if expected_revision != current["revision"]:
            raise OwnerConflict("runtime_conditions_stale")
        root = _data_root(workspace)
        assert root is not None
        scope = current["quest_ref"]
        saved = _value(scope, text, current['research_style'] if research_style is None else research_style,
                       current.get("literature_configuration") if literature_configuration is None else literature_configuration)
        with closing(sqlite3.connect(root / "meta-research.sqlite3", timeout=5.0)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            head = db.execute(
                "SELECT current_revision FROM hc_runtime_condition_heads "
                "WHERE scope_ref=?",
                (scope,),
            ).fetchone()
            if head is None or head[0] != expected_revision:
                raise OwnerConflict("runtime_conditions_stale")
            now = time.time()
            db.execute(
                "INSERT OR IGNORE INTO hc_runtime_condition_versions "
                "(scope_ref,revision,value_json,source_kind,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    scope,
                    saved["revision"],
                    json.dumps(
                        saved,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "human_update",
                    now,
                ),
            )
            changed = db.execute(
                "UPDATE hc_runtime_condition_heads SET current_revision=?,updated_at=? "
                "WHERE scope_ref=? AND current_revision=?",
                (saved["revision"], now, scope, expected_revision),
            ).rowcount
            if changed != 1:
                raise OwnerConflict("runtime_conditions_stale")
            db.commit()
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
    current = _read(root, *initial)
    public = {key: value for key, value in current.items() if key != "literature_configuration"}
    return (
        "本次调用的当前运行条件（由系统读取用户配置）：遵循下列最新条件，并传给委派的智能体。"
        "同类旧运行条件以本段为准；已经接纳的研究成果、精确输入与正式授权仍按原记录核验。\n"
        + render_research_style(current['research_style']) + '\n'
        + json.dumps(public, ensure_ascii=False, separators=(",", ":"))
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

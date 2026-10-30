"""Exact current scientific inputs with bounded optional discovery views.

The source objects remain immutable. Every omitted region is explicitly a
preview, and the authenticated stage_context reader can return its exact bytes.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

CATALOG_LIMIT = 32
CATALOG_MAX_BYTES = 24 * 1024
HISTORY_LIMIT = 12
LITERATURE_LIMIT = 24
CONTEXT_VIEW_MAX_BYTES = 64 * 1024
LOGGER = logging.getLogger(__name__)
_STAGE_SCIENTIFIC_INPUTS = {
    "idea": frozenset({"prior_accepted_bindings"}),
    "plan": frozenset(),  # The adapter supplies the exact IdeaSet once.
    "bundle": frozenset({"accepted_formal_plan_binding"}),
    "reasoning": frozenset({"plan_evidence_input", "upstream_stage_closure",
                            "accepted_target_commit_closures", "current_target_evidence_closure"}),
}
_REQUIRED_CURRENT_MATERIAL = frozenset({
    "execution_contract", "measurement_contract", "selected_input_manifest",
    "frozen_input_manifest",
})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bounded_text(value: object, max_bytes: int = 1024) -> dict[str, object]:
    text = value if isinstance(value, str) else _json(value)
    raw = text.encode("utf-8")
    return {"text": raw[:max_bytes].decode("utf-8", errors="ignore"),
            "source_utf8_bytes": len(raw), "truncated": len(raw) > max_bytes}


def bounded_list(values: list | tuple, *, limit: int = CATALOG_LIMIT,
                 max_bytes: int = CATALOG_MAX_BYTES,
                 reader: dict | None = None) -> dict[str, object]:
    items: list[object] = []
    used = 2
    for value in values[:limit]:
        cost = len(_json(value).encode("utf-8")) + (1 if items else 0)
        if used + cost > max_bytes:
            break
        items.append(value)
        used += cost
    return {"items": items, "total_count": len(values), "shown_count": len(items),
            "truncated": len(items) < len(values),
            "next_offset": len(items) if len(items) < len(values) else None,
            "reader": reader}


def scientific_handoff(outcome: dict, *, source_ref: str,
                       next_action: object = None) -> dict[str, object]:
    """A small research excerpt, explicitly distinct from the exact outcome."""
    return {"schema_ref": "meta-research/scientific-handoff-summary/v1",
            "summary_only": True, "source_ref": source_ref,
            "disposition": outcome.get("disposition"),
            "claim": bounded_text(outcome.get("claim"), 2048),
            "support_scope": bounded_text(outcome.get("support_scope"), 2048),
            "limitations": bounded_text(outcome.get("limitations", []), 2048),
            "next_action": bounded_text(next_action, 2048)}


def evidence_discovery_summary(spec: dict, metric_result: dict | None,
                               result: dict) -> dict[str, object]:
    metrics = metric_result.get("metrics", {}) if metric_result is not None else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    inputs = spec.get("semantic_inputs", [])
    purpose = [item.get("goal") for item in inputs if isinstance(item, dict)]
    result = result if isinstance(result, dict) else {}
    if isinstance(result.get("content"), dict):
        result = result["content"]
    claim = result.get("claim") or result.get("qualification_verdict") or result.get("baseline_matrix_verdict")
    result_summary = result.get("summary") or result.get("observations")
    return {"schema_ref": "meta-research/evidence-discovery-summary/v1",
            "summary_only": True, "purpose": bounded_text(purpose, 512),
            **({"claim": bounded_text(claim, 512)} if claim is not None else {}),
            **({"result_summary": bounded_text(result_summary, 512)}
               if result_summary is not None else {}),
            "support_boundary": bounded_text(result.get("limitations", []), 512),
            "metric_names": [bounded_text(key, 80)["text"] for key in list(metrics)[:12]],
            "metric_count": len(metrics)}


def literature_reference(binding: dict | None) -> dict | None:
    if binding is None:
        return None
    if "records" not in binding:
        return dict(binding)
    records = binding["records"]
    return {**{k: v for k, v in binding.items() if k != "records"},
            "kind": "QuestionLiteratureReference",
            "records_hash": hashlib.sha256(_json(records).encode()).hexdigest(),
            "record_count": len(records),
            "records_preview": bounded_list(records, limit=LITERATURE_LIMIT,
                max_bytes=4096, reader={"operation": "research_memory.stage_context.read",
                                       "source": "literature_records"})}


def stage_context_view(stage: str, context_pack: dict, *, context_pack_ref: str,
                       context_pack_hash: str, _observe: bool = True) -> dict[str, object]:
    """Construct a bounded preview without altering any frozen authority."""
    def reader(path: list[str | int]) -> dict:
        return {"operation": "research_memory.stage_context.read",
                "context_pack_ref": context_pack_ref, "source": "context_pack",
                "path": [str(key) for key in path], "offset": 0, "limit": 8192}

    def preview(value: object, path: list[str | int], depth: int = 0) -> object:
        if isinstance(value, str):
            if len(value.encode()) > 2048:
                return {**bounded_text(value, 2048), "reader": reader(path)}
            return value
        if isinstance(value, list):
            limit = LITERATURE_LIMIT if path and path[-1] == "records" else HISTORY_LIMIT
            transformed = [preview(item, path + [i], depth + 1)
                           for i, item in enumerate(value[:limit])]
            page = bounded_list(transformed, limit=limit, max_bytes=8192, reader=reader(path))
            if page["shown_count"] == len(value):
                # Complete small lists need no page wrapper; nested omissions
                # still carry their own exact reader and truncation marker.
                return transformed
            page.update(total_count=len(value), truncated=True,
                        next_offset=page["shown_count"], next_offset_unit="items")
            return page
        if isinstance(value, dict):
            if depth >= 6:
                return {"summary_only": True, "field_names": list(value)[:16],
                        "field_count": len(value), "reader": reader(path)}
            result = {}
            for key, item in value.items():
                # Proofs stay exact in the Owner object. Agents can read them
                # on demand; repeating large receipt trees is not research.
                if key in {"target_spec", "result_schema", "measurement_authority", "accepted_measurement_closures"}:
                    result[key] = {"summary_only": True, "reader": reader(path + [key])}
                elif stage == "plan" and key == "idea_set":
                    result[key] = {"provided_as": "accepted_idea_set", "reader": reader(path + [key])}
                elif "receipt" in key and isinstance(item, dict):
                    result[key] = {k: item[k] for k in ("receipt_ref", "payload_hash", "issuer", "kind") if k in item}
                else:
                    result[key] = preview(item, path + [key], depth + 1)
            return result
        return value

    sections = {}
    required_material = {}
    scientific_inputs = _STAGE_SCIENTIFIC_INPUTS.get(stage, frozenset()) | {"cycle_position"}
    for name, value in context_pack.items():
        if name in _REQUIRED_CURRENT_MATERIAL:
            required_material[name] = value
            continue
        if name in scientific_inputs or name in {"schema_ref", "cycle_ref", "foreground_epoch"}:
            sections[name] = value
        else:
            sections[name] = preview(value, [name])
    view = {"schema_ref": "meta-research/stage-context-presentation/v1",
            "stage": stage, "summary_only": True,
            "source_context_pack_ref": context_pack_ref,
            "source_context_pack_hash": context_pack_hash,
            "reader": reader([]), "sections": sections}
    view["research_notes_reader"] = {**reader([]), "operation": "research_memory.research_notes.read", "source": "research_notes",
                                     "index_offset": 0}
    from meta_research.human_research_context import human_research_reader
    view["human_guidance_reader"] = human_research_reader()
    if stage in {"idea", "plan", "bundle", "reasoning"}:
        view["formal_human_guidance_reader"] = {"operation": "human_guidance.read"}
    predecessors = context_pack.get("prior_accepted_bindings", [])
    if isinstance(predecessors, list):
        view["predecessor_research_notes_readers"] = [
            {**view["research_notes_reader"], "predecessor_ref": item["commit_ref"]}
            for item in predecessors[:HISTORY_LIMIT]
            if isinstance(item, dict) and isinstance(item.get("commit_ref"), str)
        ]
    # Small scientific notes must remain visible even when the surrounding
    # completion/proof tree is too large for a preview page. These excerpts
    # retain the exact path into the frozen source and are not new authority.
    current_notes = []
    if "prior_accepted_bindings" not in scientific_inputs and isinstance(predecessors, list):
        for index, binding in enumerate(predecessors[:3]):
            if not isinstance(binding, dict) or not isinstance(binding.get("commit_ref"), str):
                continue
            closure = binding.get("closure", {})
            note = binding.get("handoff_notes")
            if note is not None:
                original = note
                note = bounded_text(note.get("text", note) if isinstance(note, dict) else note, 2048)
                if isinstance(original, dict) and type(original.get("source_utf8_bytes")) is int:
                    note["source_utf8_bytes"] = max(note["source_utf8_bytes"], original["source_utf8_bytes"])
                    note["truncated"] = note["source_utf8_bytes"] > len(note["text"].encode())
            if note is None and isinstance(closure, dict) and isinstance(closure.get("notes"), str):
                note = bounded_text(closure["notes"], 2048)
            current_notes.append({
                "stage": "reasoning", "source_commit_ref": binding["commit_ref"],
                "source_outcome_ref": binding.get("outcome_ref"),
                "notes": note,
                "scientific_summary": bounded_text(binding["scientific_summary"], 4096)
                                      if binding.get("scientific_summary") is not None else None,
                "reader": {**reader([]), "source": "predecessor_closure",
                           "source_ref": binding["commit_ref"]},
            })
    upstream = context_pack.get("upstream_stage_closure", [])
    if "upstream_stage_closure" not in scientific_inputs and isinstance(upstream, list):
        for index in range(max(0, len(upstream) - 3), len(upstream)):
            binding = upstream[index]
            if not isinstance(binding, dict) or not isinstance(binding.get("closure"), dict):
                continue
            notes = binding["closure"].get("notes")
            if isinstance(notes, str) and notes.strip():
                current_notes.append({
                    "stage": binding.get("stage"),
                    "source_commit_ref": binding.get("commit_ref"),
                    "notes": bounded_text(notes, 2048),
                    "reader": reader(["upstream_stage_closure", index, "closure", "notes"]),
                })
            research_notes = binding["closure"].get("research_notes", [])
            if isinstance(research_notes, list):
                for note_index, note in enumerate(research_notes[:2]):
                    if isinstance(note, dict):
                        current_notes.append({
                            "stage": binding.get("stage"),
                            "source_commit_ref": binding.get("commit_ref"),
                            "research_note": preview(note, ["upstream_stage_closure", index,
                                                          "closure", "research_notes", note_index]),
                            "reader": reader(["upstream_stage_closure", index, "closure",
                                              "research_notes", note_index]),
                        })
    if current_notes:
        view["current_handoff_notes"] = current_notes
    # A few large current obligations can still exceed the aggregate budget.
    # Replace the largest previews by exact read pointers, never silently trim
    # the Owner's source or pretend the preview satisfies an evidence gate.
    def optional_view_bytes():
        return len(_json({**view, "sections": {key: value for key, value in sections.items()
                                             if key not in scientific_inputs}}).encode())

    while optional_view_bytes() > CONTEXT_VIEW_MAX_BYTES:
        candidates = [(len(_json(v).encode()), k) for k, v in sections.items()
                      if k not in scientific_inputs
                      and not (isinstance(v, dict) and v.get("omitted_from_preview"))]
        if not candidates:
            raise ValueError("context_presentation_identity_too_large")
        _, key = max(candidates)
        sections[key] = {"omitted_from_preview": True, "reader": reader([key])}
    # The context budget covers optional reading views. Current contracts and
    # selected actual input manifests retain every byte even when individually
    # larger than that budget; their size depends on this task, not all Cycles.
    if required_material:
        view["required_material"] = required_material
    if _observe:
        _observe_context_projection(stage, context_pack, view, context_pack_ref, context_pack_hash)
    return view


def stage_context_json(stage: str, context_pack: dict, *, context_pack_ref: str,
                       context_pack_hash: str) -> str:
    """Render once and reuse its actual byte representation for diagnostics."""
    view = stage_context_view(stage, context_pack, context_pack_ref=context_pack_ref,
                              context_pack_hash=context_pack_hash, _observe=False)
    rendered, section_sizes, required_size = _encode_context_view(view,
        measure=LOGGER.isEnabledFor(logging.INFO))
    _observe_context_projection(stage, context_pack, view, context_pack_ref, context_pack_hash,
        rendered=rendered, section_sizes=section_sizes, required_size=required_size)
    return rendered


def _encode_context_view(view, *, measure):
    # Sections can contain a large complete scientific handoff. Encode each
    # value once, then assemble canonical object spelling from those fragments.
    # The same fragments supply per-section sizes when diagnostics are enabled.
    sections = {name: _json(value) for name, value in view["sections"].items()}
    fields = {name: _json(value) for name, value in view.items() if name != "sections"}
    fields["sections"] = "{" + ",".join(_json(name) + ":" + sections[name]
        for name in sorted(sections)) + "}"
    rendered = "{" + ",".join(_json(name) + ":" + fields[name] for name in sorted(fields)) + "}"
    section_sizes = {name: len(value.encode("utf-8")) for name, value in sections.items()} if measure else None
    required_size = len(fields.get("required_material", "{}").encode("utf-8")) if measure else None
    return rendered, section_sizes, required_size


def _observe_context_projection(stage, source, view, source_ref, source_hash,
                                *, rendered=None, section_sizes=None, required_size=None):
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    sections = view["sections"]
    if rendered is None:
        rendered, section_sizes, required_size = _encode_context_view(view, measure=True)
    encoded_view = rendered.encode("utf-8")
    LOGGER.info("context_projection %s", _json({
        "stage": stage, "source_ref": source_ref, "source_hash": source_hash,
        "projection_hash": hashlib.sha256(encoded_view).hexdigest(),
        "default_input_utf8_bytes": len(encoded_view),
        "optional_context_budget_utf8_bytes": CONTEXT_VIEW_MAX_BYTES,
        "section_utf8_bytes": section_sizes,
        "required_material_utf8_bytes": required_size,
        "omitted_sections": [name for name, value in sections.items()
                             if isinstance(value, dict) and value.get("omitted_from_preview")],
        "compression_location": "stage_context_view",
        "exact_source_reader": view["reader"],
    }))


def context_read_page(value: object, *, path: list[str | int], offset: int,
                      limit: int) -> dict[str, object]:
    """Exact JSON text byte page, not a filesystem path or object truncation."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16384:
        raise ValueError("stage_context_page_invalid")
    if not isinstance(path, list) or len(path) > 16:
        raise ValueError("stage_context_path_invalid")
    node: Any = value
    for key in path:
        if isinstance(node, dict) and isinstance(key, str) and key in node:
            node = node[key]
        elif isinstance(node, list) and (type(key) is int or isinstance(key, str) and key.isdecimal()) and 0 <= int(key) < len(node):
            node = node[int(key)]
        else:
            raise ValueError("stage_context_path_invalid")
    raw = _json(node).encode("utf-8")
    if offset > len(raw) or (offset < len(raw) and raw[offset] & 0xC0 == 0x80):
        raise ValueError("stage_context_offset_invalid")
    chunk = raw[offset:offset + limit]
    text = chunk.decode("utf-8", errors="ignore")
    consumed = len(text.encode())
    if not consumed and offset < len(raw):
        raise ValueError("stage_context_limit_too_small")
    return {"text": text, "encoding": "canonical-json-utf8-bytes",
            "content_hash": hashlib.sha256(raw).hexdigest(), "path": path,
            "offset": offset, "offset_unit": "utf8_bytes", "returned_bytes": consumed, "total_bytes": len(raw),
            "next_offset": offset + consumed if offset + consumed < len(raw) else None,
            "complete": offset == 0 and consumed == len(raw)}


def reasoning_handoff_reference(binding: dict) -> dict[str, object]:
    closure = binding.get("closure", {})
    return {"kind": "ReasoningHandoffReference", "cycle_ref": binding["cycle_ref"],
            "commit_ref": binding["commit_ref"], "outcome_ref": binding["outcome_ref"],
            "closure_hash": hashlib.sha256(_json(closure).encode()).hexdigest(),
            "receipt": binding["receipt"], "outcome_receipt": binding["outcome_receipt"],
            "scientific_summary": closure.get("scientific_summary"),
            **({"handoff_notes": {"text": closure["notes"],
                                  "source_utf8_bytes": len(closure["notes"].encode("utf-8")),
                                  "truncated": False}}
               if isinstance(closure.get("notes"), str) and closure["notes"].strip() else {}),
            **({"research_notes": closure["research_notes"]} if closure.get("research_notes") else {}),
            "exact_reader": "research_memory.stage_context.read"}


def stage_position_prompt(request, stage: str) -> str:
    """Render only the immutable position bound to this exact request."""
    from meta_research.cycle_position import validate_cycle_position
    position = request.context_pack.get("cycle_position")
    validate_cycle_position(position, cycle_ref=request.context_pack["cycle_ref"])
    return "cycle_position=" + _json({**position, "stage": stage}) + "\n"

"""Verified replacement of a Bundle provider identity at a quiescent graph boundary."""
import json

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref
from meta_research.runtime_conditions import read_runtime_conditions_in_transaction

CAPABILITY = "replace_bundle_provider_runtime"
ACTION = "bundle_provider_transition"


def preview(connection, control, run, attempt, session, previous, current, units):
    from meta_research.bundle_runtime_refresh import _same_execution_scope

    conditions = read_runtime_conditions_in_transaction(connection, control.quest_ref)
    graph = connection.execute(text(
        "SELECT graph_ref,receipt_ref,receipt_hash FROM rg_target_graphs WHERE run_ref=:run "
        "AND request_ref=:request AND cycle_ref=:cycle AND quest_ref=:quest"
    ), {"run": run.run_ref, "request": run.request_ref, "cycle": run.cycle_ref, "quest": control.quest_ref}).one_or_none()
    head = connection.execute(text(
        "SELECT 1 FROM ae_foreground_heads WHERE quest_ref=:quest AND cycle_ref=:cycle "
        "AND stage='bundle' AND status='active' AND epoch=:epoch"
    ), {"quest": control.quest_ref, "cycle": run.cycle_ref, "epoch": run.epoch}).first()
    completed = [u for u in units if u.attempt_ref == attempt.attempt_ref and u.status == "completed"]
    if (graph is None or head is None or not completed or control.cleanup_status != "none"
            or conditions.get("stage_models", {}).get("bundle") != current.model_ref
            or previous.model_ref == current.model_ref
            or not _same_execution_scope(previous, current, authorized_provider_change=True)):
        raise OwnerConflict("bundle_provider_transition_not_ready")
    latest = max(completed, key=lambda unit: (unit.completed_at, unit.unit_ref))
    scope = {
        "schema_ref": "meta-research/bundle-provider-transition/v1",
        "quest_ref": control.quest_ref, "cycle_ref": run.cycle_ref, "run_ref": run.run_ref,
        "stage_request_ref": run.request_ref, "attempt_ref": attempt.attempt_ref,
        "fence_ref": attempt.fence_ref, "generation": int(attempt.generation),
        "conditions_revision": conditions["revision"], "native_session_ref": session.native_session_ref,
        "graph_ref": graph.graph_ref, "graph_receipt_ref": graph.receipt_ref,
        "graph_receipt_hash": graph.receipt_hash,
        "last_completed_unit_ref": latest.unit_ref, "last_completed_operation_ref": latest.operation_ref,
        "from_model": previous.model_ref, "to_model": current.model_ref,
        "from_harness": previous.harness_adapter_ref, "to_harness": current.harness_adapter_ref,
        "from_shared_codex_adapter": [v for v in previous.resource_bindings
                                      if v.startswith("adapter-source:meta_research.idea_skill@sha256:")],
        "to_shared_codex_adapter": [v for v in current.resource_bindings
                                    if v.startswith("adapter-source:meta_research.idea_skill@sha256:")],
        "from_binding_hash": run.runtime_binding_hash, "to_binding_hash": canonical_hash(current.as_dict()),
        "successor_native_session": "new_from_committed_graph",
    }
    return {"scope_ref": "bundle_provider_transition:" + canonical_hash(attempt.attempt_ref),
            "requirement": {"capability": CAPABILITY, "scope": scope}}


V2_SCOPE = "meta-research/bundle-provider-transition/v2"


def _binding_changes(previous, current):
    before, after = previous.as_dict(), current.as_dict()
    result = {}
    for key, old in before.items():
        new = after[key]
        if old == new:
            continue
        if isinstance(old, list):
            result[key] = {"removed": [v for v in old if v not in new], "added": [v for v in new if v not in old],
                           "from_hash": canonical_hash(old), "to_hash": canonical_hash(new)}
        else:
            result[key] = {"from": old, "to": new}
    return result


def _unexecuted_execution_scope(previous, current):
    # Explicit code changes may replace an existing implementation at the same role.
    # Preserve the provider, native executable, tools, transport seal, sandbox, and actual permissions.
    def resource_names(binding):
        names = []
        for item in binding.resource_bindings:
            name, sep, digest = item.rpartition("@sha256:")
            if (sep and name.startswith("adapter-source:meta_research.") and len(digest) == 64
                    and all(c in "0123456789abcdef" for c in digest)):
                names.append(name)
            else:
                names.append(item)
        return names

    def permissions(binding):
        return [v for v in binding.capability_bindings if not v.startswith("root-capability-profile:sha256:")]

    return (previous.schema_ref == current.schema_ref and previous.model_ref == current.model_ref
            and previous.harness_adapter_ref == current.harness_adapter_ref and previous.mcp_bindings == current.mcp_bindings
            and permissions(previous) == permissions(current) and resource_names(previous) == resource_names(current))


def _inherited_terminal(connection, run_ref, checkpoint, inherited):
    if inherited is None:
        source = checkpoint
    else:
        source = connection.execute(text("SELECT * FROM ar_safe_points WHERE run_ref=:run AND safe_point_ref=:point"),
            {"run": run_ref, "point": inherited["safe_point_ref"]}).one_or_none()
        if source is None or source.checkpoint_hash != inherited["checkpoint_hash"]:
            raise OwnerConflict("bundle_provider_transition_checkpoint_invalid")
    point = json.loads(source.checkpoint_json)
    scope = point["authorization"]["requirement"]["scope"]
    if (canonical_hash(point) != source.checkpoint_hash or point.get("action") != ACTION
            or scope.get("schema_ref") != "meta-research/bundle-provider-transition/v1"
            or not point.get("retired_native_session_ref") or point["retired_native_session_ref"] != scope.get("native_session_ref")
            or scope.get("run_ref") != run_ref or scope.get("attempt_ref") != source.attempt_ref
            or scope.get("fence_ref") != source.fence_ref):
        raise OwnerConflict("bundle_provider_transition_checkpoint_invalid")
    unit = connection.execute(text("SELECT * FROM ar_provider_units WHERE run_ref=:run AND unit_ref=:unit "
        "AND attempt_ref=:attempt AND fence_ref=:fence AND operation_ref=:op AND status='completed' AND completed_at IS NOT NULL"),
        {"run":run_ref, "unit":scope["last_completed_unit_ref"], "attempt":source.attempt_ref,
         "fence":source.fence_ref, "op":scope["last_completed_operation_ref"]}).one_or_none()
    if unit is None or point.get("provider_unit_ref") != unit.unit_ref or point.get("provider_operation_ref") != unit.operation_ref:
        raise OwnerConflict("bundle_provider_transition_terminal_evidence_invalid")
    selector = "revision=:revision" if inherited is not None else "json_extract(payload_json,'$.unit_ref')=:unit"
    events = connection.execute(text("SELECT revision,payload_json FROM durable_feed WHERE "
        "event_type='agent_runtime.provider_unit_safe' AND " + selector),
        {"unit":unit.unit_ref, "revision":None if inherited is None else inherited["safe_event_revision"]}).all()
    expected = {"unit_ref":unit.unit_ref, "run_ref":run_ref, "status":"completed"}
    if len(events) != 1 or json.loads(events[0].payload_json) != expected:
        raise OwnerConflict("bundle_provider_transition_terminal_evidence_invalid")
    result = {"safe_point_ref":source.safe_point_ref,"checkpoint_hash":source.checkpoint_hash,
        "originating_attempt_ref":source.attempt_ref,"originating_fence_ref":source.fence_ref,"originating_generation":scope["generation"],
        "originating_native_session_ref":scope["native_session_ref"], "terminal_operation_ref":unit.operation_ref,
        "terminal_unit_ref":unit.unit_ref,"terminal_status":unit.status,"completed_at":unit.completed_at,
        "safe_event_revision":events[0].revision,"safe_event_hash":canonical_hash(expected)}
    if inherited is not None and result != inherited:
        raise OwnerConflict("bundle_provider_transition_terminal_evidence_invalid")
    return result


def preview_unexecuted(owner, connection, control, run, attempt, session, previous, current, units):
    from meta_research.bundle_runtime_refresh import _ledger

    conditions = read_runtime_conditions_in_transaction(connection, control.quest_ref)
    params = {"run":run.run_ref, "request":run.request_ref, "quest":control.quest_ref, "cycle":run.cycle_ref,
              "epoch":run.epoch, "attempt":attempt.attempt_ref, "fence":attempt.fence_ref}
    graph = connection.execute(text("SELECT g.* FROM rg_target_graphs g JOIN ae_foreground_heads h ON h.quest_ref=g.quest_ref "
        "AND h.cycle_ref=g.cycle_ref WHERE g.run_ref=:run AND g.request_ref=:request AND g.quest_ref=:quest AND g.cycle_ref=:cycle "
        "AND h.epoch=:epoch AND h.stage='bundle' AND h.status='active'"),params).one_or_none()
    invocations = connection.execute(text("SELECT * FROM ar_stage_provider_invocations WHERE attempt_ref=:attempt"),params).all()
    unresolved = connection.execute(text("SELECT 1 FROM ar_execution_responsibilities WHERE root_run_ref=:run "
        "AND (status!='finished' OR attempt_ref=:attempt OR fence_ref=:fence) LIMIT 1"),params).first()
    if (graph is None or unresolved is not None or session.native_session_ref is not None or control.cleanup_status != "none"
            or attempt.status != "running" or attempt.submission_ref is not None or attempt.primary_draft_hash is not None
            or attempt.primary_draft_json is not None or attempt.payload_hash is not None
            or conditions["stage_models"]["bundle"] != current.model_ref or not _unexecuted_execution_scope(previous,current)
            or any(u.attempt_ref == attempt.attempt_ref or u.fence_ref == attempt.fence_ref for u in units)
            or len(invocations) != 1):
        raise OwnerConflict("bundle_provider_transition_unexecuted_not_ready")
    inv = invocations[0]
    if (inv.run_ref != run.run_ref or inv.fence_ref != attempt.fence_ref or inv.phase != "primary" or inv.status != "prepared"
            or inv.completed_at is not None or inv.response_hash is not None or inv.runtime_binding_hash != run.runtime_binding_hash):
        raise OwnerConflict("bundle_provider_transition_unexecuted_not_ready")
    history = _ledger(connection,run.run_ref)
    if not history or history[-1]["replacement_attempt_ref"] != attempt.attempt_ref or "provider_authorization" not in history[-1]:
        raise OwnerConflict("bundle_provider_transition_checkpoint_invalid")
    parent = history[-1]
    row = replacement_checkpoint(connection,run.run_ref,attempt.attempt_ref,attempt.fence_ref)
    prior_grant = parent["provider_authorization"]
    prior_proposal = {key:prior_grant[key] for key in ("scope_ref","requirement")}
    prior_scope = prior_proposal["requirement"]["scope"]
    if (row is None or row.safe_point_ref != parent["provider_transition"]["safe_point_ref"]
            or parent["runtime_binding_hash"] != run.runtime_binding_hash or parent["root_session_ref"] != session.session_ref
            or prior_scope["graph_ref"] != graph.graph_ref or prior_scope["graph_receipt_hash"] != graph.receipt_hash
            or authorize(owner,connection,prior_proposal,prior_grant["authorization_receipt_ref"]) != prior_grant):
        raise OwnerConflict("bundle_provider_transition_checkpoint_invalid")
    terminal = _inherited_terminal(connection,run.run_ref,row,prior_scope.get("inherited_terminal"))
    heads = connection.execute(text("SELECT generation,receipt_ref,receipt_hash FROM rg_target_graph_appends "
        "WHERE graph_ref=:g ORDER BY generation DESC LIMIT 1"),{"g":graph.graph_ref}).first()
    commits = connection.execute(text("SELECT c.target_ref,c.commit_ref,c.receipt_ref,c.receipt_hash,c.closure_hash "
        "FROM rg_target_commits c JOIN rg_targets t ON t.target_ref=c.target_ref WHERE t.graph_ref=:g ORDER BY c.target_ref"),
        {"g":graph.graph_ref}).mappings().all()
    scope = {"schema_ref":V2_SCOPE,"quest_ref":control.quest_ref,"cycle_ref":run.cycle_ref,"run_ref":run.run_ref,
        "stage_request_ref":run.request_ref,"request_receipt_ref":run.request_receipt_ref,"request_receipt_hash":run.request_receipt_hash,
        "context_pack_ref":run.context_pack_ref,"context_pack_hash":run.context_pack_hash,
        "attempt_ref":attempt.attempt_ref,"fence_ref":attempt.fence_ref,"generation":int(attempt.generation),
        "root_session_ref":session.session_ref,"native_session_ref":None,"conditions_revision":conditions["revision"],
        "graph_ref":graph.graph_ref,"graph_receipt_ref":graph.receipt_ref,"graph_receipt_hash":graph.receipt_hash,
        "graph_head":dict(heads._mapping) if heads else {"generation":0,"receipt_ref":graph.receipt_ref,"receipt_hash":graph.receipt_hash},
        "committed_targets":[dict(c) for c in commits],
        "unexecuted_primary":{"invocation_ref":inv.invocation_ref,"operation_ref":inv.operation_ref,"request_hash":inv.request_hash,
                              "status":"prepared","physical_units":0},
        "previous_handoff":{"refresh_ref":parent["refresh_ref"],"safe_point_ref":row.safe_point_ref,"checkpoint_hash":row.checkpoint_hash,
                "authorization_receipt_ref":prior_grant["authorization_receipt_ref"],"authorization_receipt_hash":prior_grant["authorization_receipt_hash"],
                "confirmation_receipt_ref":prior_grant["confirmation_receipt_ref"],"confirmation_receipt_hash":prior_grant["confirmation_receipt_hash"]},
        "inherited_terminal":terminal,"from_model":previous.model_ref,"to_model":current.model_ref,
        "from_harness":previous.harness_adapter_ref,"to_harness":current.harness_adapter_ref,
        "from_binding_hash":run.runtime_binding_hash,"to_binding_hash":canonical_hash(current.as_dict()),
        "binding_material_diff":_binding_changes(previous,current),"successor_native_session":"new_from_committed_graph"}
    return {"scope_ref":"bundle_provider_transition:"+canonical_hash(attempt.attempt_ref),
            "requirement":{"capability":CAPABILITY,"scope":scope}}

def authorize(owner, connection, proposal, receipt_ref):
    row = connection.execute(text(
        "SELECT authorization_ref,receipt_hash,basis_confirmation_ref,basis_confirmation_hash "
        "FROM hc_capability_authorizations WHERE receipt_ref=:receipt AND scope_ref=:scope "
        "AND is_current=1 AND decision='granted' AND capability=:capability"
    ), {"receipt": receipt_ref, "scope": proposal["scope_ref"], "capability": CAPABILITY}).one_or_none()
    if row is None:
        raise OwnerConflict("bundle_provider_transition_authorization_invalid")
    owner._authorization_verifier.verify_capability_authorization(
        requirement=proposal["requirement"], receipt_ref=receipt_ref)
    return {**proposal, "authorization_ref": row.authorization_ref,
            "authorization_receipt_ref": receipt_ref, "authorization_receipt_hash": row.receipt_hash,
            "confirmation_receipt_ref": row.basis_confirmation_ref,
            "confirmation_receipt_hash": row.basis_confirmation_hash}


def record_checkpoint(connection, grant, session_ref, now):
    scope = grant["requirement"]["scope"]
    checkpoint = {"action": ACTION, "run_ref": scope["run_ref"], "attempt_ref": scope["attempt_ref"],
                  "fence_ref": scope["fence_ref"], "root_session_ref": session_ref,
                  "retired_native_session_ref": scope["native_session_ref"], "authorization": grant}

    if scope.get("schema_ref") == V2_SCOPE:
        checkpoint["inherited_terminal"] = scope["inherited_terminal"]
    else:
        checkpoint["provider_operation_ref"] = scope["last_completed_operation_ref"]
        checkpoint["provider_unit_ref"] = scope["last_completed_unit_ref"]

    ref, digest = new_ref("safe_point"), canonical_hash(checkpoint)
    connection.execute(text(
        "INSERT INTO ar_safe_points (safe_point_ref,operation_ref,run_ref,attempt_ref,root_session_ref,"
        "fence_ref,checkpoint_json,checkpoint_hash,created_at) VALUES (:ref,:op,:run,:attempt,:root,"
        ":fence,:payload,:hash,:now)"
    ), {"ref": ref, "op": grant["authorization_ref"], "run": scope["run_ref"], "attempt": scope["attempt_ref"],
        "root": session_ref, "fence": scope["fence_ref"], "payload": canonical_json(checkpoint),
        "hash": digest, "now": now})
    return {"safe_point_ref": ref, "checkpoint_hash": digest}


def replacement_checkpoint(connection, run_ref, attempt_ref, fence_ref):
    from meta_research.owners.agent_runtime import _CORRECTION_ANCESTRY_SQL
    from meta_research.bundle_runtime_refresh import _ledger

    row = connection.execute(text(_CORRECTION_ANCESTRY_SQL +
        "SELECT s.*,r.request_ref,r.context_pack_ref,r.context_pack_hash,c.status AS control_status,"
        "x.replacement_attempt_ref FROM correction_ancestors a "
        "JOIN ar_bundle_runtime_refreshes x ON x.run_ref=a.run_ref AND x.replacement_attempt_ref=a.attempt_ref "
        "JOIN ar_safe_points s ON s.safe_point_ref=json_extract(x.payload_json,'$.provider_transition.safe_point_ref') "
        "JOIN ar_stage_runs r ON r.run_ref=x.run_ref JOIN ar_run_controls c ON c.run_ref=r.run_ref "
        "WHERE c.attempt_ref=:attempt AND c.fence_ref=:fence ORDER BY a.depth LIMIT 1"
    ), {"run": run_ref, "attempt": attempt_ref, "fence": fence_ref}).first()
    if row is None:
        return None
    entry = next((e for e in _ledger(connection, run_ref)
                  if e["replacement_attempt_ref"] == row.replacement_attempt_ref), None)
    point = json.loads(row.checkpoint_json)
    if (entry is None or row.control_status != "running" or point.get("action") != ACTION
            or entry["provider_transition"] != {"safe_point_ref": row.safe_point_ref, "checkpoint_hash": row.checkpoint_hash}
            or point.get("authorization") != entry["provider_authorization"]
            or point.get("retired_native_session_ref") != entry["native_session_ref"]
            or row.attempt_ref != entry["retired_attempt_ref"] or row.fence_ref != entry["retired_fence_ref"]
            or row.root_session_ref != entry["root_session_ref"] or row.run_ref != run_ref
            or canonical_hash(point) != row.checkpoint_hash):
        raise OwnerConflict("bundle_provider_transition_checkpoint_invalid")
    if entry["provider_authorization"]["requirement"]["scope"].get("schema_ref") == V2_SCOPE:
        inherited = entry["provider_authorization"]["requirement"]["scope"].get("inherited_terminal")
        if not isinstance(inherited,dict) or point.get("inherited_terminal") != inherited or entry["native_session_ref"] is not None:
            raise OwnerConflict("bundle_provider_transition_checkpoint_invalid")
        _inherited_terminal(connection,run_ref,row,inherited)
    return row

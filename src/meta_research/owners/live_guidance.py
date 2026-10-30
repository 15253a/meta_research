"""Immutable supplements to one operation's original guidance cut."""
from dataclasses import replace
import time

from sqlalchemy import text

from meta_research.human_guidance import FrozenGuidanceDelivery, GuidanceOperationIdentity
from meta_research.owners.common import canonical_hash, canonical_json, decoded_object, new_ref
from meta_research.owners.human_collaboration_ladder import guidance_binding_from_row


def prepare_live_guidance(owner, *, scope, binding):
    from meta_research.owners.human_guidance import _cut, guidance_applies_to_work
    owner.verify_frozen_guidance_binding(binding)
    work = owner._agent_runtime.verify_guidance_read_scope(scope, binding)
    original = owner.recover_operation_guidance(binding.identity)
    with owner._database.fenced_write() as connection:
        owner._agent_runtime.verify_guidance_read_scope(scope, binding)
        supplements = _supplements(connection, binding)
        known = {decoded_object(item.guide_json)["constraint_ref"]: item
                 for item in original.deliveries if item.applies_to_work}
        for row in supplements:
            for item in _cut(row).deliveries:
                known[decoded_object(item.guide_json)["constraint_ref"]] = item
        rows = connection.execute(text(
            "SELECT * FROM hc_soft_constraints WHERE scope_ref=:scope ORDER BY created_at,constraint_ref"
        ), {"scope": "quest:" + binding.quest_ref}).all()
        messages = []
        for row in rows:
            guide = guidance_binding_from_row(row)
            semantic_scope = guide["guidance"].get("semantic_scope")
            if semantic_scope is None:
                continue
            prior = known.get(row.constraint_ref)
            applies = guidance_applies_to_work(connection, semantic_scope, work)
            if not applies and prior is None:
                continue
            if row.status != "active" and prior is None:
                continue  # Never delivered, withdrawn guidance has no influence.
            if (row.status == "active" and any(
                    decoded_object(item.guide_json) == guide for item in original.deliveries)):
                continue
            change = {"guide": guide, "status": row.status if applies else "outside_scope"}
            identity = GuidanceOperationIdentity(binding.identity.root_kind,
                binding.identity.run_ref, "guidance_update_" + canonical_hash({
                    "parent": binding.as_dict(), "change": change}))
            stored = connection.execute(text(
                "SELECT * FROM hc_guidance_snapshots WHERE root_kind=:root_kind "
                "AND run_ref=:run_ref AND operation_ref=:operation_ref"
            ), vars(identity)).first()
            if stored is None:
                deliveries = []
                if change["status"] == "active":
                    deliveries = [FrozenGuidanceDelivery(new_ref("guidance_delivery"),
                        canonical_json(guide), True, None, True, "confirmed")]
                original_row = connection.execute(text(
                    "SELECT snapshot_json FROM hc_guidance_snapshots WHERE snapshot_ref=:ref"
                ), {"ref": binding.snapshot_ref}).one()
                initial = decoded_object(original_row.snapshot_json)
                payload = {"identity": vars(identity), "quest_ref": binding.quest_ref,
                    "provenance_ref": original.provenance_ref, "operation": initial["operation"],
                    "parent_binding": binding.as_dict(), "guidance_change": change,
                    "deliveries": [item.as_dict() for item in deliveries],
                    "direction_cut": original.direction_cut}
                snapshot_ref, now = new_ref("guidance_snapshot"), time.time()
                connection.execute(text(
                    "INSERT INTO hc_guidance_snapshots VALUES "
                    "(:snapshot_ref,:root_kind,:run_ref,:operation_ref,:quest_ref,:json,:hash,:now)"
                ), {**vars(identity), "snapshot_ref": snapshot_ref, "quest_ref": binding.quest_ref,
                    "json": canonical_json(payload), "hash": canonical_hash(payload), "now": now})
                for delivery in deliveries:
                    connection.execute(text(
                        "INSERT INTO hc_guidance_deliveries (delivery_ref,snapshot_ref,constraint_ref,"
                        "revision,guidance_hash,needs_treatment,created_at) "
                        "VALUES (:delivery_ref,:snapshot_ref,:constraint_ref,:revision,:guidance_hash,1,:now)"
                    ), {"delivery_ref": delivery.delivery_ref, "snapshot_ref": snapshot_ref,
                        "constraint_ref": row.constraint_ref, "revision": row.revision,
                        "guidance_hash": row.guidance_hash, "now": now})
                stored = connection.execute(text(
                    "SELECT * FROM hc_guidance_snapshots WHERE snapshot_ref=:ref"
                ), {"ref": snapshot_ref}).one()
            frozen = _cut(stored)
            messages.append({"kind": "confirmed_guidance_change", "change": change,
                "prepared_at": stored.created_at,
                "binding": binding.as_dict(), "supplement": frozen.binding.as_dict(),
                "deliveries": [item.as_dict() for item in frozen.deliveries],
                "instruction": "这是当前原根内新正式确认的指导变化。说明相对于此前已收到指导的变化；"
                    "按其确认范围和力度处理。撤回不自动停止实验、删除成果或回滚目标。"
                    "outside_scope表示原已收到指导现已退出本根适用范围，不继续当作本工作要求。"
                    "原初始输入保持；需要反馈时使用本消息准确delivery_ref。"})
        return messages


def _supplements(connection, binding):
    rows = connection.execute(text(
        "SELECT * FROM hc_guidance_snapshots WHERE root_kind=:root_kind AND run_ref=:run_ref "
        "AND operation_ref LIKE 'guidance_update_%' ORDER BY created_at,snapshot_ref"
    ), vars(binding.identity)).all()
    return [row for row in rows if decoded_object(row.snapshot_json).get("parent_binding") == binding.as_dict()]


def with_live_deliveries(owner, cut):
    from meta_research.owners.human_guidance import _cut
    with owner._database.read() as connection:
        rows = _supplements(connection, cut.binding)
    changes = {value["guide"]["constraint_ref"]: value
        for row in rows if isinstance(value := decoded_object(row.snapshot_json).get("guidance_change"), dict)}
    deliveries = tuple(cut.deliveries) + tuple(item for row in rows for item in _cut(row).deliveries)
    current = []
    for item in deliveries:
        guide = decoded_object(item.guide_json)
        change = changes.get(guide["constraint_ref"])
        status = "active" if change is None else change["status"]
        if status == "active" and change is not None and change["guide"] != guide:
            status = "superseded"
        current.append(item if status == "active" else replace(item,
            needs_treatment=False, applies_to_work=False, guidance_status=status))
    return replace(cut, deliveries=tuple(current))

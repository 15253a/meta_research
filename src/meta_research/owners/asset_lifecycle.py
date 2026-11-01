from __future__ import annotations

import json
import time

from sqlalchemy import text

from meta_research.dataset_contract import dataset_asset_binding
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    new_ref,
)


def _description(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        raise OwnerConflict("asset_change_" + field + "_required")
    return value.strip()


def validate_asset_change(change, asset_ref):
    if change is None:
        if asset_ref is not None:
            raise OwnerConflict("asset_change_required")
        return None
    if not asset_ref or not isinstance(change, dict):
        raise OwnerConflict("asset_change_invalid")
    allowed = {
        "kind",
        "predecessor_version_ref",
        "expected_revision",
        "explanation",
        "error",
        "scope",
        "evidence_bindings",
        "impact",
        "no_affected_work_explanation",
    }
    if set(change) - allowed or change.get("kind") not in {
        "supplement",
        "substantive_change",
        "correction",
    }:
        raise OwnerConflict("asset_change_invalid")
    if change["kind"] != "correction" and set(change) & {
        "error",
        "scope",
        "evidence_bindings",
        "impact",
        "no_affected_work_explanation",
    }:
        raise OwnerConflict("asset_change_correction_fields_invalid")
    if (
        type(change.get("expected_revision")) is not int
        or change["expected_revision"] < 0
    ):
        raise OwnerConflict("asset_revision_invalid")
    result = {
        **change,
        "explanation": _description(change.get("explanation"), "explanation"),
        "predecessor_version_ref": _description(
            change.get("predecessor_version_ref"), "predecessor"
        ),
    }
    if change["kind"] == "correction":
        result["error"] = _description(change.get("error"), "error")
        result["scope"] = _description(change.get("scope"), "scope")
        evidence = change.get("evidence_bindings")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 32:
            raise OwnerConflict("asset_correction_evidence_required")
        result["evidence_bindings"] = [
            dataset_asset_binding(value).as_dict() for value in evidence
        ]
        impact = change.get("impact", [])
        if not isinstance(impact, list) or len(impact) > 256:
            raise OwnerConflict("asset_correction_impact_invalid")
        if not impact or "no_affected_work_explanation" in change:
            result["no_affected_work_explanation"] = _description(
                change.get("no_affected_work_explanation"),
                "no_affected_work_explanation",
            )
        result["impact"] = []
        for item in impact:
            if (
                not isinstance(item, dict)
                or set(item) != {"work_ref", "judgment", "explanation"}
                or item["judgment"] not in {"unaffected", "recheck", "redo", "unknown"}
            ):
                raise OwnerConflict("asset_correction_impact_invalid")
            result["impact"].append(
                {
                    "work_ref": _description(item["work_ref"], "work_ref"),
                    "judgment": item["judgment"],
                    "explanation": _description(item["explanation"], "impact"),
                }
            )
    return result


def assert_asset_usable(connection, version_ref):
    version = connection.execute(
        text("SELECT asset_ref FROM rm_asset_versions WHERE version_ref=:ref"),
        {"ref": version_ref},
    ).first()
    if version is None:
        raise OwnerConflict("asset_not_found")
    _, _, states = _verified_lifecycle(connection, version.asset_ref)
    if states.get(version_ref) == "retired":
        raise OwnerConflict("asset_version_retired", {"version_ref": version_ref})


def assert_asset_payload_usable(connection, value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"version_ref", "asset_version_ref", "memory_ref"} and isinstance(
                item, str
            ):
                exists = connection.execute(
                    text("SELECT 1 FROM rm_asset_versions WHERE version_ref=:ref"),
                    {"ref": item},
                ).first()
                if exists is not None:
                    assert_asset_usable(connection, item)
            else:
                assert_asset_payload_usable(connection, item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_asset_payload_usable(connection, item)


def assert_scientific_asset_sources_usable(connection, document):
    pending = [document]
    sources = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif isinstance(value, str):
            sources.add(value)
    for source in sorted(sources):
        exists = connection.execute(
            text("SELECT 1 FROM rm_asset_versions WHERE version_ref=:ref"),
            {"ref": source},
        ).first()
        if exists is not None:
            assert_asset_usable(connection, source)


def retained_asset_references(connection, version_ref):
    references = []
    target_rows = (
        connection.execute(
            text(
                "SELECT t.target_ref FROM rg_targets t WHERE EXISTS (SELECT 1 FROM json_each(t.spec_json, '$.candidate.direct_accepted_input_asset_refs') j WHERE j.value=:ref) ORDER BY t.target_ref"
            ),
            {"ref": version_ref},
        )
        .scalars()
        .all()
    )
    references.extend(f"rg_targets:{ref}" for ref in target_rows)
    direct = (
        ("rm_target_implementation_artifacts", "implementation_revision_ref"),
        ("rm_target_implementation_bundles", "implementation_revision_ref"),
        ("rm_target_input_asset_proofs", "proof_ref"),
        ("rm_target_research_notes", "note_ref"),
        ("rg_writing_citation_decisions", "decision_ref"),
    )
    for table, key in direct:
        rows = (
            connection.execute(
                text(
                    f"SELECT {key} FROM {table} WHERE version_ref=:ref ORDER BY {key}"
                ),
                {"ref": version_ref},
            )
            .scalars()
            .all()
        )
        references.extend(f"{table}:{ref}" for ref in rows)
    structured = (
        ("rm_creation_bases", "basis_ref", "body_json"),
        ("rm_target_generic_result_manifests", "manifest_ref", "payload_json"),
        ("rm_asset_changes", "change_ref", "payload_json"),
        ("rm_target_root_completion_manifests", "manifest_ref", "entries_json"),
        ("hc_research_inputs", "input_ref", "payload_json"),
        ("hc_quest_initializations", "initialization_id", "draft_json"),
        ("hc_manual_question_creations", "context_ref", "seed_json"),
        ("hc_deepfetch_requests", "request_ref", "material_bindings_json"),
        ("hc_manual_deepfetch_requests", "request_ref", "material_bindings_json"),
        ("hc_human_request_responses", "response_ref", "facts_json"),
    )
    for table, key, column in structured:
        rows = (
            connection.execute(
                text(
                    f"SELECT t.{key} FROM {table} t WHERE EXISTS (SELECT 1 FROM json_tree(t.{column}) j WHERE j.key IN ('version_ref','asset_version_ref','memory_ref') AND j.value=:ref) ORDER BY t.{key}"
                ),
                {"ref": version_ref},
            )
            .scalars()
            .all()
        )
        references.extend(f"{table}:{ref}" for ref in rows)
    return references


def _accepted_change(row):
    payload = json.loads(row.payload_json)
    if canonical_hash(payload) != row.request_hash:
        raise OwnerConflict("asset_change_receipt_invalid")
    expected = canonical_hash(
        {
            "change_ref": row.change_ref,
            "asset_ref": row.asset_ref,
            "version_ref": row.version_ref,
            "predecessor_version_ref": row.predecessor_version_ref,
            "kind": row.kind,
            "payload": payload,
            "revision": row.revision,
            "accepted_at": row.accepted_at,
        }
    )
    if expected != row.receipt_hash:
        raise OwnerConflict("asset_change_receipt_invalid")
    if (
        payload.get("kind") != row.kind
        or payload.get("predecessor_version_ref") != row.predecessor_version_ref
    ):
        raise OwnerConflict("asset_change_receipt_invalid")
    return {
        **payload,
        "change_ref": row.change_ref,
        "asset_ref": row.asset_ref,
        "version_ref": row.version_ref,
        "predecessor_version_ref": row.predecessor_version_ref,
        "revision": row.revision,
        "accepted_at": row.accepted_at,
        "receipt": AcceptanceReceipt(
            "research_memory",
            "asset_lifecycle_change",
            row.receipt_ref,
            row.change_ref,
            row.receipt_hash,
        ).as_public_dict(),
    }


def _verified_lifecycle(connection, asset_ref):
    head = connection.execute(
        text("SELECT * FROM rm_asset_lifecycle WHERE asset_ref=:ref"),
        {"ref": asset_ref},
    ).first()
    rows = connection.execute(
        text("SELECT * FROM rm_asset_changes WHERE asset_ref=:ref ORDER BY revision"),
        {"ref": asset_ref},
    ).all()
    changes = [_accepted_change(row) for row in rows]
    versions = connection.execute(
        text(
            "SELECT v.version_ref,COALESCE(l.state,'unselected') AS state FROM rm_asset_versions v LEFT JOIN rm_asset_version_lifecycle l USING(version_ref) WHERE v.asset_ref=:ref"
        ),
        {"ref": asset_ref},
    ).all()
    states = {row.version_ref: "unselected" for row in versions}
    current = None
    for revision, change in enumerate(changes, 1):
        if change["revision"] != revision or change["version_ref"] not in states:
            raise OwnerConflict("asset_lifecycle_state_invalid")
        version_ref = change["version_ref"]
        if change["kind"] == "retirement":
            states[version_ref] = "retired"
            if current == version_ref:
                current = None
        else:
            predecessor = change["predecessor_version_ref"]
            if predecessor is not None:
                if (
                    predecessor not in states
                    or states[predecessor] == "retired"
                    or current is not None
                    and current != predecessor
                ):
                    raise OwnerConflict("asset_lifecycle_state_invalid")
                states[predecessor] = "superseded"
            current = version_ref
    if head is None:
        if changes:
            raise OwnerConflict("asset_lifecycle_state_invalid")
    elif head.revision != len(changes) or head.current_version_ref != current:
        raise OwnerConflict("asset_lifecycle_state_invalid")
    if any(row.state != states[row.version_ref] for row in versions):
        raise OwnerConflict("asset_lifecycle_state_invalid")
    return head, changes, states


def _retirement_payload(
    memory_ref,
    *,
    expected_revision,
    expected_reference_revision,
    explanation,
    low_value,
    obsolete,
    incorrect,
    impact_understood,
    has_explanation_value,
    idempotency_key,
):
    explanation = _description(explanation, "explanation")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or len(idempotency_key) > 128
    ):
        raise OwnerConflict("asset_retirement_idempotency_key_invalid")
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or type(expected_reference_revision) is not int
        or expected_reference_revision < 0
    ):
        raise OwnerConflict("asset_revision_invalid")
    judgments = {
        "low_value": low_value,
        "obsolete": obsolete,
        "incorrect": incorrect,
        "impact_understood": impact_understood,
        "has_explanation_value": has_explanation_value,
    }
    if any(type(v) is not bool for v in judgments.values()):
        raise OwnerConflict("asset_retirement_judgment_invalid")
    payload = {
        "kind": "retirement",
        "version_ref": memory_ref,
        "expected_revision": expected_revision,
        "expected_reference_revision": expected_reference_revision,
        "explanation": explanation,
        **judgments,
    }
    return payload


class AssetLifecycleOwnerMixin:
    def query_asset_status(self, asset_ref, version_ref=None):
        """Read the selected version's present applicability, without full history.

        Lifecycle writes still verify the complete chain. Ordinary reads verify
        the head and the accepted facts affecting this version; the detailed
        endpoint remains available for the entire historical chain.
        """
        with self._database.read_snapshot() as connection:
            head = connection.execute(text('SELECT * FROM rm_asset_lifecycle WHERE asset_ref=:ref'),
                {'ref': asset_ref}).first()
            latest = connection.execute(text('SELECT * FROM rm_asset_changes WHERE asset_ref=:ref ORDER BY revision DESC LIMIT 1'),
                {'ref': asset_ref}).first()
            creation = connection.execute(text("SELECT * FROM rm_asset_changes WHERE asset_ref=:ref AND kind!='retirement' ORDER BY revision DESC LIMIT 1"),
                {'ref': asset_ref}).first()
            verified = {}
            def fact(row):
                if row.change_ref not in verified:
                    verified[row.change_ref] = _accepted_change(row)
                return verified[row.change_ref]
            if latest is not None:
                fact(latest)
            # Owner-managed scientific contents join the asset inventory
            # without an explicit lifecycle selection. A missing head is only
            # valid when there are no lifecycle changes, as in the full reader.
            if (head is None and latest is not None
                or head is not None and head.revision != (latest.revision if latest else 0)):
                raise OwnerConflict('asset_lifecycle_state_invalid')
            current = None
            if creation is not None:
                fact(creation)
                retirement = connection.execute(text("SELECT * FROM rm_asset_changes WHERE asset_ref=:asset AND version_ref=:version AND kind='retirement' ORDER BY revision DESC LIMIT 1"),
                    {'asset': asset_ref, 'version': creation.version_ref}).first()
                if retirement is not None:
                    fact(retirement)
                else:
                    current = creation.version_ref
            if head is not None and head.current_version_ref != current:
                raise OwnerConflict('asset_lifecycle_state_invalid')
            if version_ref is None:
                version_ref = current
                if version_ref is None:
                    version_ref = connection.execute(text('SELECT version_ref FROM rm_asset_versions WHERE asset_ref=:ref ORDER BY version_number DESC LIMIT 1'),
                        {'ref': asset_ref}).scalar()
            row = connection.execute(text("SELECT v.version_ref,COALESCE(l.state,'unselected') AS state FROM rm_asset_versions v LEFT JOIN rm_asset_version_lifecycle l USING(version_ref) WHERE v.asset_ref=:asset AND v.version_ref=:version"),
                {'asset': asset_ref, 'version': version_ref}).first()
            if row is None:
                raise OwnerConflict('asset_not_found')
            rows = connection.execute(text('SELECT * FROM rm_asset_changes WHERE asset_ref=:asset AND (version_ref=:version OR predecessor_version_ref=:version) ORDER BY revision'),
                {'asset': asset_ref, 'version': version_ref}).all()
            changes = [fact(item) for item in rows]
            state = 'unselected'
            for change in changes:
                if change['kind'] == 'retirement' and change['version_ref'] == version_ref:
                    state = 'retired'
                elif change['predecessor_version_ref'] == version_ref:
                    state = 'superseded'
            if row.state != state:
                raise OwnerConflict('asset_lifecycle_state_invalid')
            return {'asset_ref': asset_ref, 'revision': head.revision if head else 0,
                'current_version_ref': current, 'version_ref': version_ref,
                'state': 'current' if current == version_ref else state,
                'applicability': [{key: change[key] for key in
                    ('kind', 'explanation', 'error', 'scope', 'change_ref', 'version_ref', 'predecessor_version_ref') if key in change}
                    for change in changes],
                'history_reader': {'operation': 'research_memory.assets.lifecycle',
                    'args': {'version_ref': version_ref}}}

    def query_asset_lifecycle(self, asset_ref):
        with self._database.read_snapshot() as connection:
            head, changes, states = _verified_lifecycle(connection, asset_ref)
            if head is None:
                asset = connection.execute(
                    text("SELECT asset_ref FROM rm_assets WHERE asset_ref=:ref"),
                    {"ref": asset_ref},
                ).first()
                if asset is None:
                    raise OwnerConflict("asset_not_found")
            versions = connection.execute(
                text(
                    "SELECT v.version_ref,COALESCE(l.state,'unselected') AS state FROM rm_asset_versions v LEFT JOIN rm_asset_version_lifecycle l USING(version_ref) WHERE v.asset_ref=:ref ORDER BY v.version_number"
                ),
                {"ref": asset_ref},
            ).all()
            by_version = {v.version_ref: [] for v in versions}
            predecessors, successors = {}, {}
            for change in changes:
                by_version[change['version_ref']].append(change)
                predecessor = change['predecessor_version_ref']
                if predecessor is not None and predecessor != change['version_ref']:
                    by_version[predecessor].append(change)
                if change['kind'] != 'retirement':
                    predecessors.setdefault(change['version_ref'], predecessor)
                    if predecessor is not None:
                        successors.setdefault(predecessor, []).append(change['version_ref'])
            return {
                "asset_ref": asset_ref,
                "revision": head.revision if head else 0,
                "current_version_ref": head.current_version_ref if head else None,
                "changes": changes,
                "versions": [
                    {
                        "version_ref": v.version_ref,
                        "state": "current"
                        if head and head.current_version_ref == v.version_ref
                        else states[v.version_ref],
                        "predecessor_version_ref": predecessors.get(v.version_ref),
                        "successor_version_refs": successors.get(v.version_ref, []),
                        "changes": by_version[v.version_ref],
                    }
                    for v in versions
                ],
            }

    def query_current_asset(self, asset_ref):
        with self._database.read_snapshot():
            state = self.query_asset_status(asset_ref)
            ref = state["current_version_ref"]
            return None if ref is None else self.query_asset_version(ref)

    def _record_asset_change(
        self, connection, *, asset_ref, version_ref, revision, payload, idempotency_key
    ):
        change_ref, receipt_ref, now = (
            new_ref("asset_change"),
            new_ref("rm_lifecycle_receipt"),
            time.time(),
        )
        values = {
            "change_ref": change_ref,
            "asset_ref": asset_ref,
            "version_ref": version_ref,
            "predecessor_version_ref": payload.get("predecessor_version_ref"),
            "revision": revision,
            "kind": payload["kind"],
            "payload_json": canonical_json(payload),
            "request_hash": canonical_hash(payload),
            "idempotency_key": idempotency_key,
            "receipt_ref": receipt_ref,
            "receipt_hash": canonical_hash(
                {
                    "change_ref": change_ref,
                    "asset_ref": asset_ref,
                    "version_ref": version_ref,
                    "predecessor_version_ref": payload.get("predecessor_version_ref"),
                    "kind": payload["kind"],
                    "payload": payload,
                    "revision": revision,
                    "accepted_at": now,
                }
            ),
            "accepted_at": now,
        }
        connection.execute(
            text(
                "INSERT INTO rm_asset_changes ("
                + ",".join(values)
                + ") VALUES ("
                + ",".join(":" + key for key in values)
                + ")"
            ),
            values,
        )
        row = connection.execute(
            text("SELECT * FROM rm_asset_changes WHERE change_ref=:ref"),
            {"ref": change_ref},
        ).first()
        return _accepted_change(row)

    def _accept_asset_lifecycle(
        self,
        connection,
        *,
        asset_ref,
        version_ref,
        change,
        origin_quest_ref=None,
        idempotency_key,
    ):
        connection.execute(
            text("INSERT OR IGNORE INTO rm_asset_lifecycle(asset_ref) VALUES (:ref)"),
            {"ref": asset_ref},
        )
        head, _, _ = _verified_lifecycle(connection, asset_ref)
        if change is None:
            if head.revision != 0:
                raise OwnerConflict("asset_change_required")
            payload = {
                "kind": "initial",
                "explanation": "Initial accepted current version.",
            }
            if origin_quest_ref is not None:
                quest = connection.execute(
                    text("SELECT 1 FROM rg_quests WHERE quest_ref=:ref"),
                    {"ref": origin_quest_ref},
                ).first()
                if quest is None:
                    raise OwnerConflict("asset_origin_quest_invalid")
                payload["origin_quest_ref"] = origin_quest_ref
        else:
            if head.revision != change["expected_revision"]:
                raise OwnerConflict("asset_revision_stale")
            predecessor = connection.execute(
                text("SELECT asset_ref FROM rm_asset_versions WHERE version_ref=:ref"),
                {"ref": change["predecessor_version_ref"]},
            ).first()
            if predecessor is None or predecessor.asset_ref != asset_ref:
                raise OwnerConflict("asset_change_predecessor_invalid")
            if (
                head.current_version_ref is not None
                and head.current_version_ref != change["predecessor_version_ref"]
            ):
                raise OwnerConflict("asset_change_predecessor_stale")
            assert_asset_usable(connection, change["predecessor_version_ref"])
            for value in change.get("evidence_bindings", []):
                binding = dataset_asset_binding(value)
                assert_asset_usable(connection, binding.version_ref)
                self.verify_asset_receipt(
                    asset_ref=binding.asset_ref,
                    version_ref=binding.version_ref,
                    content_hash=binding.content_hash,
                    manifest_hash=binding.manifest_hash,
                    receipt=binding.receipt,
                )
            payload = change
            connection.execute(
                text(
                    "INSERT INTO rm_asset_version_lifecycle(version_ref,state) VALUES (:ref,'superseded') ON CONFLICT(version_ref) DO UPDATE SET state='superseded'"
                ),
                {"ref": change["predecessor_version_ref"]},
            )
        connection.execute(
            text(
                "INSERT INTO rm_asset_version_lifecycle(version_ref,state) VALUES (:ref,'unselected')"
            ),
            {"ref": version_ref},
        )
        revision = head.revision + 1
        connection.execute(
            text(
                "UPDATE rm_asset_lifecycle SET revision=:revision,current_version_ref=:version WHERE asset_ref=:asset"
            ),
            {"revision": revision, "version": version_ref, "asset": asset_ref},
        )
        self._record_asset_change(
            connection,
            asset_ref=asset_ref,
            version_ref=version_ref,
            revision=revision,
            payload=payload,
            idempotency_key="intake:" + idempotency_key,
        )

    def query_retirement_by_idempotency_key(
        self, memory_ref, *, idempotency_key, **judgment
    ):
        payload = _retirement_payload(
            memory_ref, idempotency_key=idempotency_key, **judgment
        )
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rm_asset_changes WHERE idempotency_key=:key"),
                {"key": "retire:" + idempotency_key},
            ).first()
            if row is None:
                return None
            if row.request_hash != canonical_hash(payload):
                raise OwnerConflict("asset_retirement_idempotency_conflict")
            return _accepted_change(row)

    def retire_asset_version(
        self,
        memory_ref,
        *,
        expected_revision,
        expected_reference_revision,
        explanation,
        low_value,
        obsolete,
        incorrect,
        impact_understood,
        has_explanation_value,
        idempotency_key,
        effect_scope=None,
    ):
        payload = _retirement_payload(
            memory_ref,
            expected_revision=expected_revision,
            expected_reference_revision=expected_reference_revision,
            explanation=explanation,
            low_value=low_value,
            obsolete=obsolete,
            incorrect=incorrect,
            impact_understood=impact_understood,
            has_explanation_value=has_explanation_value,
            idempotency_key=idempotency_key,
        )
        with self._database.fenced_write() as connection:
            if effect_scope is not None:
                effect_scope()
            replay = connection.execute(
                text("SELECT * FROM rm_asset_changes WHERE idempotency_key=:key"),
                {"key": "retire:" + idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != canonical_hash(payload):
                    raise OwnerConflict("asset_retirement_idempotency_conflict")
                return _accepted_change(replay)
            asset = connection.execute(
                text("SELECT * FROM rm_asset_versions WHERE version_ref=:ref"),
                {"ref": memory_ref},
            ).first()
            if asset is None:
                raise OwnerConflict("asset_not_found")
            self.verify_asset_receipt(
                asset_ref=asset.asset_ref,
                version_ref=asset.version_ref,
                content_hash=asset.content_hash,
                manifest_hash=asset.manifest_hash,
                receipt=AcceptanceReceipt(
                    "research_memory",
                    asset.acceptance_kind,
                    asset.receipt_ref,
                    asset.version_ref,
                    asset.receipt_hash,
                ),
            )
            head, _, _ = _verified_lifecycle(connection, asset.asset_ref)
            if (head.revision if head else 0) != expected_revision:
                raise OwnerConflict("asset_revision_stale")
            assert_asset_usable(connection, memory_ref)
            reasons = []
            if not low_value or not obsolete or not incorrect:
                reasons.append("retirement_basis_insufficient")
            if not impact_understood:
                reasons.append("impact_uncertain")
            if has_explanation_value:
                reasons.append("explanation_value_retained")
            if asset.acceptance_kind != "asset_acceptance":
                reasons.append("owner_content_retained")
            if self._reference_reader is None:
                raise OwnerConflict("reference_state_uncertain")
            (
                reference_revision,
                references,
            ) = self._reference_reader.query_asset_reference_state(memory_ref)
            references = tuple(
                sorted(
                    set(
                        [
                            *references,
                            *retained_asset_references(connection, memory_ref),
                            *self._retained_scientific_asset_references(
                                connection, memory_ref
                            ),
                        ]
                    )
                )
            )
            if reference_revision != expected_reference_revision:
                reasons.append("reference_revision_stale")
            if references:
                reasons.append("active_references")
            holds = [
                hold.hold_ref
                for hold in self.query_asset_holds(memory_ref)
                if hold.active
            ]
            if holds:
                reasons.append("active_holds")
            if reasons:
                raise OwnerConflict(
                    "asset_retirement_blocked",
                    {
                        "reasons": reasons,
                        "active_reference_refs": list(references),
                        "active_hold_refs": holds,
                        "observed_reference_revision": reference_revision,
                    },
                )
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO rm_asset_lifecycle(asset_ref) VALUES (:ref)"
                ),
                {"ref": asset.asset_ref},
            )
            connection.execute(
                text(
                    "INSERT INTO rm_asset_version_lifecycle(version_ref,state) VALUES (:ref,'retired') ON CONFLICT(version_ref) DO UPDATE SET state='retired'"
                ),
                {"ref": memory_ref},
            )
            connection.execute(
                text(
                    "UPDATE rm_asset_lifecycle SET revision=revision+1,current_version_ref=CASE WHEN current_version_ref=:version THEN NULL ELSE current_version_ref END WHERE asset_ref=:asset"
                ),
                {"version": memory_ref, "asset": asset.asset_ref},
            )
            result = self._record_asset_change(
                connection,
                asset_ref=asset.asset_ref,
                version_ref=memory_ref,
                revision=expected_revision + 1,
                payload=payload,
                idempotency_key="retire:" + idempotency_key,
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision=revision+1 WHERE singleton='owner'"
                )
            )
            self._feed.record(
                connection,
                "research_memory.asset_retired",
                {"version_ref": memory_ref, "change_ref": result["change_ref"]},
            )
            return result

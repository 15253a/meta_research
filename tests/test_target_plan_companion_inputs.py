"""Selected historical results authorize exact companion inputs across Cycles."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_target_input_reference_bridge import _authority


def _companion_owner(refs=("asset_companion",), *, commits=None):
    owner = _authority(refs, plan=True)
    template = owner._domain_reader.query_asset_roles(version_refs=("version_1",))[0]
    role = replace(template, role_ref="role_companion", asset_ref="asset_companion",
        version_ref="asset_version_companion",
        asset_receipt=replace(template.asset_receipt, subject_ref="asset_version_companion"))
    assets = commits if commits is not None else {"commit_1": (role.asset_binding(),), "commit_2": ()}
    requests, accepted, issued = [], {}, []
    query_roles = owner._domain_reader.query_asset_roles
    owner._domain_reader.query_asset_roles = lambda **kw: (
        *query_roles(**kw), *((role,) if role.version_ref in kw["version_refs"] else ()))
    owner._domain_reader.verify_asset_quest_scope = lambda ref, **kw: role.asset_binding()

    def read(*, quest_ref, target_commit_refs):
        assert quest_ref == "quest"
        requests.append(target_commit_refs)
        return {ref: assets.get(ref, ()) for ref in target_commit_refs}

    owner._domain_reader.query_target_commit_input_asset_bindings = read
    owner._domain_reader.accept_asset_role = lambda **kw: issued.append(kw) or role
    owner._memory.accept_input_asset = lambda **kw: role.asset_receipt
    owner.query_input_asset_projection = lambda **kw: accepted.get(kw["asset_ref"])

    def accept(**kw):
        with owner._database.read() as db:
            db.execute(text("INSERT INTO rg_target_input_asset_role_proofs VALUES (:target, :asset, :key)"),
                {"target": kw["target_ref"], "asset": kw["role"].asset_ref, "key": kw["idempotency_key"]})
            db.commit()
        accepted[role.asset_ref] = SimpleNamespace(asset=role.asset_binding(),
            source_role_ref=role.role_ref, source_role_receipt=role.receipt)
        return accepted[role.asset_ref]

    owner.accept_input_asset_role = accept
    return owner, role, accepted, issued, requests

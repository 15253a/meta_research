"""Canonical Bundle input records shared by dispatch and exact readers."""
from meta_research.owners.common import OwnerConflict
from meta_research.target_commit_evidence import target_commit_metric_result


def target_projection(target):
    candidate = target.spec.get("candidate")
    risk_class = target.spec.get("risk_class")
    if not isinstance(candidate, dict) or risk_class not in {"normal", "high"}:
        raise OwnerConflict("target_dispatch_formal_spec_invalid")
    return {
        "target_ref": target.target_ref,
        "target_key": target.target_key,
        "spec_hash": target.spec_hash,
        "spec": target.spec,
        "candidate": candidate,
        "risk_class": risk_class,
        "dependency_refs": list(target.dependency_refs),
        "receipt": target.receipt.as_public_dict(),
    }


def target_commit_projection(commit):
    return {
        "commit_ref": commit.commit_ref,
        "target_ref": commit.target_ref,
        "target_run_ref": commit.target_run_ref,
        "evaluation_attempt_ref": commit.evaluation_attempt_ref,
        "target_spec_hash": commit.target_spec_hash,
        "closure_hash": commit.closure_hash,
        **({"result_disposition": commit.result_disposition}
           if commit.result_disposition is not None else {}),
        "protocol": commit.closure["protocol"],
        "metric_result": target_commit_metric_result(commit),
        "receipt": commit.receipt.as_public_dict(),
    }

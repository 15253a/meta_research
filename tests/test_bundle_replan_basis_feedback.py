import copy

import pytest

from meta_research.bundle_skill import (
    BundleSkillContractError,
    BundleTargetBatchResult,
    validate_bundle_target_batch_result,
)
from test_bundle_batch_access import _bind
from test_bundle_input_drift_recovery import _adapter, _output


def _valid_request(tmp_path):
    adapter, _ = _adapter(tmp_path, _output("dispatch"), durable=True)
    request, *_ = _bind(adapter)
    return request


def _result(request, strategy_update, replan_basis):
    return BundleTargetBatchResult(
        strategy_update=strategy_update,
        rationale="x",
        native_session_ref=request.native_session_ref,
        adapter_kind="codex_cli",
        action="propose_targets",
        replan_basis=replan_basis,
    )


def _strategy_update(request):
    update = copy.deepcopy(_output("batch")["strategy_update"])
    update["requires_accepted_labels"] = [request.target_commits[0]["commit_ref"]]
    return update


def test_batch_rejects_non_null_replan_basis(tmp_path):
    request = _valid_request(tmp_path)
    result = _result(request, _strategy_update(request), {})
    with pytest.raises(
        BundleSkillContractError,
        match="bundle_propose_targets_replan_basis_not_null",
    ):
        validate_bundle_target_batch_result(request, result)


def test_batch_null_strategy_update_is_generic(tmp_path):
    request = _valid_request(tmp_path)
    result = _result(request, None, {})
    with pytest.raises(
        BundleSkillContractError,
        match="bundle_target_batch_invalid",
    ):
        validate_bundle_target_batch_result(request, result)


def test_batch_rejects_consumed_unaccepted_result(tmp_path):
    request = _valid_request(tmp_path)
    result = _result(request, _strategy_update(request), None)
    with pytest.raises(
        BundleSkillContractError,
        match="strategy_consumed_unaccepted_result",
    ):
        validate_bundle_target_batch_result(request, result)

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from meta_research.owners.advancement_engine import SQLiteAdvancementEngine
from meta_research.owners.common import OwnerConflict, accepted_idea_set_binding_from_public
from test_plan_skill_contract import _IDEA_BINDING


def verifier(origin='cycle-1'):
    binding = accepted_idea_set_binding_from_public(deepcopy(_IDEA_BINDING))
    commit = SimpleNamespace(cycle_ref=origin, stage='idea', outcome_kind='idea_set',
        outcome_ref=binding.outcome_ref, outcome_receipt=binding.outcome_receipt,
        receipt=binding.stage_commit_receipt)
    owner = object.__new__(SQLiteAdvancementEngine)
    owner._database = MagicMock()
    owner._outcome_verifier = Mock()
    owner._stage_commit_from_row = Mock(return_value=commit)
    owner.query_reasoning_successor_context = Mock(return_value={
        'cycle_ref': 'cycle-3', 'source_cycle_ref': 'cycle-2', 'entry_stage': 'plan',
        'accepted_idea_set_binding': binding.as_dict()})
    return owner, binding, commit


@pytest.mark.parametrize('origin', ['cycle-1', 'cycle-2', 'cycle-3'])
def test_exact_idea_reuse_can_span_multiple_accepted_successors(origin):
    owner, binding, _ = verifier(origin)
    owner._verify_plan_idea_set('cycle-3', binding)
    owner._outcome_verifier.verify_accepted_idea_set_binding.assert_called_once_with(binding)


@pytest.mark.parametrize('field,value', [('entry_stage','idea'), ('accepted_idea_set_binding',{}), ('cycle_ref','unrelated-cycle')])
def test_inherited_idea_requires_exact_authenticated_successor(field, value):
    owner, binding, _ = verifier()
    owner.query_reasoning_successor_context.return_value[field] = value
    with pytest.raises(OwnerConflict, match='plan_idea_set_stage_commit_invalid'):
        owner._verify_plan_idea_set('cycle-3', binding)


@pytest.mark.parametrize('field', ['stage','outcome_kind','outcome_ref','outcome_receipt','receipt'])
def test_original_idea_commit_checks_remain_exact(field):
    owner, binding, commit = verifier()
    setattr(commit, field, 'wrong')
    with pytest.raises(OwnerConflict, match='plan_idea_set_stage_commit_invalid'):
        owner._verify_plan_idea_set('cycle-3', binding)


def test_reuse_cannot_bypass_rejected_successor_proof():
    owner, binding, _ = verifier()
    owner.query_reasoning_successor_context.side_effect = OwnerConflict('reasoning_successor_skip_commits_invalid')
    with pytest.raises(OwnerConflict, match='reasoning_successor_skip_commits_invalid'):
        owner._verify_plan_idea_set('cycle-3', binding)

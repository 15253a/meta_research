"""Small deterministic synthesis fixtures for Owner-focused tests."""
from copy import deepcopy
from meta_research.creation_basis import FirstQuestionSynthesisResult


def synthesis_result(request, content, adapter_kind, companion_native_session_ref=None,
                     proposal_fork_native_session_ref=None):
    revision = None
    if request.literature_snapshot is not None:
        revision = {"understanding": deepcopy(request.basis["understanding"]),
                    "corrections": [],
                    "search_assessment": "The supplied literature does not change this fixture's prepared understanding."}
    return FirstQuestionSynthesisResult(content, adapter_kind, companion_native_session_ref,
                                        proposal_fork_native_session_ref, revision)

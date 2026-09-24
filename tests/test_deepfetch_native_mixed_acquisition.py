from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from meta_research.deepfetch import CodexDeepFetchAdapter
from test_deepfetch_adapter import (
    PROTOTYPE_ACQUIRE,
    PROTOTYPE_FINAL,
    RecordingAcquisitionClient,
    _bind_acquisition,
    _request,
)
from test_deepfetch_native_acquisition import NativeAcquisitionRunner


class MixedAcquisitionRunner(NativeAcquisitionRunner):
    """The model repeats its successful native request in the turn envelope."""

    def __init__(self, workspace: Path, authority: RecordingAcquisitionClient) -> None:
        super().__init__(workspace, authority)
        final = copy.deepcopy(PROTOTYPE_FINAL)
        final["workflow"]["finalized_at"] = None
        self.outputs = [copy.deepcopy(PROTOTYPE_ACQUIRE), final]

    def __call__(self, argv, prompt, timeout):
        research_turns = sum(
            "web_evidence_gate=v1" not in recorded_prompt
            for _argv, recorded_prompt, _timeout in self.calls
        )
        self.commit = self.emit = research_turns == 0
        return super().__call__(argv, prompt, timeout)


def test_native_acquisition_repeated_in_envelope_reuses_the_same_owner_effect(tmp_path: Path) -> None:
    workspace = tmp_path / "provider"
    authority = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    runner = MixedAcquisitionRunner(workspace, authority)
    adapter = _bind_acquisition(
        CodexDeepFetchAdapter(workspace, model_ref="gpt-test", process_runner=runner),
        authority,
    )
    request = replace(
        _request(), runtime_binding=adapter.runtime_binding(),
        job_ref="deepfetch-native-and-envelope-acquisition",
    )

    result = adapter.execute(request)

    assert result.completion == "complete"
    assert len(result.fulltexts) == 1
    assert len(authority.calls) == 1
    assert len(runner.calls) == 3
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert len(checkpoint["acquisition_request_ids"]) == 1
    assert len(checkpoint["acquisition_item_proofs"]) == 1
    assert checkpoint["acquisition_item_proofs"][0]["phase"] == "turn-1"
    assert checkpoint["acquisition_item_proofs"][0]["effect_id"] == "acq-v4-1"

    replay = adapter.execute(
        replace(request, reconcile_only=True, native_session_ref=result.native_session_ref)
    )

    assert replay == result
    assert len(authority.calls) == 1
    assert len(runner.calls) == 3

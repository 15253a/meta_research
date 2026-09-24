from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research import deepfetch as deepfetch_module
from meta_research.composition import build_production_runtime
from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from test_public_first_question_deepfetch import (
    PROTOTYPE_EMPTY_FINAL,
    PROTOTYPE_EMPTY_LEDGER,
    DeterministicProbe,
    RecordingAcquisitionProvider,
    SnapshotAwareProposalDrafter,
    _authenticate,
    _open_and_queue_deepfetch,
    _write_headers,
)


def _provider_executable(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

arguments = sys.argv[1:]
if 'exec' not in arguments:
    raise SystemExit(0)
counter = pathlib.Path(__file__).with_suffix('.count')
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
prompt = sys.stdin.read()
native = arguments[-2] if 'resume' in arguments else 'native-finalization-recovery'
print(json.dumps({'type': 'thread.started', 'thread_id': native}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'search', 'type': 'web_search', 'query': 'paper',
    'action': {'type': 'search'}}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'fetch', 'type': 'web_search', 'query': '',
    'action': {'type': 'other'}}}), flush=True)
result = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
if 'web_evidence_gate=v1' in prompt:
    result.write_text(json.dumps({'status': 'web_evidence_ready'}))
    raise SystemExit(0)
public = pathlib.Path(next(line.split('=', 1)[1] for line in prompt.splitlines()
                           if line.startswith('public_output_root=')))
(public / 'fulltext').mkdir(parents=True, exist_ok=True)
(public / 'papers.json').write_text(json.dumps(__LEDGER__, ensure_ascii=False), encoding='utf-8')
(public / 'summary.md').write_text('# 范围\\n\\n本轮未形成可纳入的精确论文。\\n', encoding='utf-8')
result.write_text(json.dumps(__FINAL__), encoding='utf-8')
""".replace("__LEDGER__", repr(PROTOTYPE_EMPTY_LEDGER)).replace(
            "__FINAL__", repr(PROTOTYPE_EMPTY_FINAL)
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("binding_changed", [False, True])
@pytest.mark.parametrize("tamper_spool", [False, True])
def test_failed_finalization_reconciles_sealed_operation_without_new_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_changed: bool,
    tamper_spool: bool,
) -> None:
    executable = _provider_executable(tmp_path / "fake-codex")
    data_root = prepare_data_root(tmp_path / "data")
    workspace = data_root.run / "deepfetch-provider"
    adapter = CodexDeepFetchAdapter(
        workspace, executable=str(executable), model_ref="gpt-test"
    )
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=adapter,
        acquisition_provider=RecordingAcquisitionProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, headers, key_prefix="finalization-recovery"
        )
        request_ref = queued["deepfetch"]["request_ref"]
        importer = deepfetch_module._import_v4_public_artifacts

        def reject_metadata(*args, **kwargs):
            raise DeepFetchUnavailable("deepfetch_workflow_evidence_invalid")

        monkeypatch.setattr(
            deepfetch_module, "_import_v4_public_artifacts", reject_metadata
        )
        assert runtime.deepfetch.process_once()
        failed = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert failed["deepfetch"]["failure"] == {
            "code": "deepfetch_workflow_evidence_invalid"
        }
        first_run = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert first_run is not None
        assert first_run.provider_operation_retry_permitted
        old_binding_hash = first_run.runtime_binding_hash
        with runtime._database.read() as connection:
            first_attempt = dict(connection.execute(text(
                "SELECT * FROM ar_deepfetch_attempts WHERE attempt_ref = :ref"
            ), {"ref": first_run.attempt_ref}).mappings().one())
        provider_invocations = executable.with_suffix(".count").read_text()
        assert int(provider_invocations) >= 2
        if tamper_spool:
            invocation_path = next(workspace.glob(
                "provider-operations/*/*/invocation.json"
            ))
            invocation = json.loads(invocation_path.read_text())
            invocation["seal"] = "0" * 64
            invocation_path.write_text(json.dumps(invocation), encoding="utf-8")
        preserved = {
            path: path.read_bytes()
            for folder in ("provider-operations", "research-workspaces")
            for path in (workspace / folder).rglob("*")
            if path.is_file()
        }
        public_files = {
            path: path.read_bytes()
            for path in workspace.glob("runs/*/public/**/*")
            if path.is_file()
        }
        assert preserved and public_files
        monkeypatch.setattr(deepfetch_module, "_import_v4_public_artifacts", importer)
        if binding_changed:
            # A deployed skill revision changes the provider binding while the
            # old invocation, schema, native session and signed results survive.
            adapter._skill_bundle_hash = "f" * 64
            assert canonical_hash(adapter.runtime_binding().as_dict()) != old_binding_hash
        retried = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
            headers=_write_headers(headers, "finalization-recovery-retry"),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retried.raise_for_status()
        assert retried.json()["deepfetch"]["request_ref"] == request_ref
        progressed = runtime.deepfetch.process_once()
        after = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert executable.with_suffix(".count").read_text() == provider_invocations
        assert all(path.is_file() and path.read_bytes() == value
                   for path, value in {**preserved, **public_files}.items())
        final_run = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert final_run is not None
        assert final_run.run_ref == first_run.run_ref
        assert final_run.root_session_ref == first_run.root_session_ref
        assert final_run.native_session_ref == first_run.native_session_ref
        assert final_run.runtime_binding_hash == old_binding_hash
        assert final_run.provider_operation_ref == first_run.provider_operation_ref
        assert final_run.provider_operation_generation == 1
        with runtime._database.read() as connection:
            unchanged_attempt = dict(connection.execute(text(
                "SELECT * FROM ar_deepfetch_attempts WHERE attempt_ref = :ref"
            ), {"ref": first_run.attempt_ref}).mappings().one())
        assert unchanged_attempt == first_attempt
        if tamper_spool:
            assert not progressed
            assert after["deepfetch"]["status"] == "queued"
            assert final_run.status == "admitted"
            assert final_run.execution_receipt is None
            time.sleep(0.6)
            assert not runtime.deepfetch.process_once()
            assert executable.with_suffix(".count").read_text() == provider_invocations
            assert all(path.is_file() and path.read_bytes() == value
                       for path, value in {**preserved, **public_files}.items())
        else:
            assert progressed
            assert after["deepfetch"]["status"] == "succeeded"
            assert final_run.status == "executed"
            assert final_run.attempt_generation == 2
            assert final_run.execution_receipt is not None
            with runtime._database.read() as connection:
                proposal = connection.execute(text(
                    "SELECT status, literature_snapshot_ref FROM "
                    "hc_proposal_generation_attempts WHERE initialization_id = :ref"
                ), {"ref": initialization_id}).one()
            assert proposal.status == "queued"
            assert proposal.literature_snapshot_ref is not None
        events = runtime.feed.read_after(0).events
        recovery = next(event for event in events if event.event_type ==
                        "agent_runtime.deepfetch_result_reconciliation_started")
        assert recovery.payload["previous_attempt_ref"] == first_run.attempt_ref
        assert recovery.payload["provider_operation_ref"] == first_run.provider_operation_ref
        assert not any(event.event_type ==
                       "agent_runtime.deepfetch_runtime_binding_transitioned"
                       for event in events)
    finally:
        client.close()
        runtime.close()


def test_other_verified_terminal_failure_still_starts_new_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _provider_executable(tmp_path / "fake-codex")
    data_root = prepare_data_root(tmp_path / "data")
    adapter = CodexDeepFetchAdapter(
        data_root.run / "deepfetch-provider",
        executable=str(executable),
        model_ref="gpt-test",
    )
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=adapter,
        acquisition_provider=RecordingAcquisitionProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, headers, key_prefix="other-terminal-retry"
        )
        request_ref = queued["deepfetch"]["request_ref"]
        importer = deepfetch_module._import_v4_public_artifacts

        def reject_artifact(*args, **kwargs):
            raise DeepFetchUnavailable("deepfetch_papers_v4_validator_failed")

        monkeypatch.setattr(
            deepfetch_module, "_import_v4_public_artifacts", reject_artifact
        )
        assert runtime.deepfetch.process_once()
        first = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert first is not None and first.provider_operation_retry_permitted
        invocations = int(executable.with_suffix(".count").read_text())
        monkeypatch.setattr(deepfetch_module, "_import_v4_public_artifacts", importer)
        failed = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        retried = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
            headers=_write_headers(headers, "other-terminal-retry-again"),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retried.raise_for_status()
        assert runtime.deepfetch.process_once()
        final = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert final is not None and final.status == "executed"
        assert final.provider_operation_generation == 2
        assert final.provider_operation_ref != first.provider_operation_ref
        assert int(executable.with_suffix(".count").read_text()) == invocations + 2
        assert not any(
            event.event_type == "agent_runtime.deepfetch_result_reconciliation_started"
            for event in runtime.feed.read_after(0).events
        )
    finally:
        client.close()
        runtime.close()

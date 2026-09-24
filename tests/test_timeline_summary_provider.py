from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from meta_research.quest_drafting import DraftingUnavailable
from meta_research.timeline_summary_provider import CodexTimelineSummaryAdapter, _output_schema


def nodes():
    return [
        {
            "node_key": "question:question-1", "kind": "question",
            "question_ref": "question-1", "cycle_ref": None, "stage": None, "target_ref": None,
            "source_hash": "a" * 64,
            "content": {"unknown_statement": "脑电分类效果能否跨被试泛化？", "details": "完整材料" * 600},
            "sources": [{"ref": "question-1", "label": "研究问题"}],
        },
        {
            "node_key": "target:target-1", "kind": "target",
            "question_ref": "question-1", "cycle_ref": "cycle-1", "stage": "bundle", "target_ref": "target-1",
            "source_hash": "b" * 64,
            "content": {"goal": "按被试划分数据，排查训练集与测试集重叠", "finding": "尚不能判定泛化有效"},
            "sources": [{"ref": "target-1"}, {"ref": "progress-1"}],
            "previous_summary": "正在核验数据划分依据。",
        },
    ]


def output():
    return {"next_scan_seconds": 900, "summaries": [
        {"node_key": "question:question-1", "source_hash": "a" * 64,
         "summary": "研究脑电分类效果能否跨被试泛化，并明确验证范围。", "source_refs": ["question-1"]},
        {"node_key": "target:target-1", "source_hash": "b" * 64,
         "summary": "核查按被试划分的数据是否重叠，现有证据尚不能判定泛化有效。", "source_refs": ["progress-1"]},
    ]}


class RecorderRunner:
    def __init__(self, value=None):
        self.output = output() if value is None else value
        self.calls = []
        self.schemas = []
        self.version_calls = 0

    def run_command(self, argv, timeout):
        self.version_calls += 1
        return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.156.1\n", stderr="")

    def __call__(self, argv, prompt, timeout):
        self.calls.append((argv, prompt))
        self.schemas.append(json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text()))
        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(self.output))
        return subprocess.CompletedProcess(argv, 0,
                                          stdout=json.dumps({"type": "thread.started", "thread_id": "recorder-native-1"}),
                                          stderr="")


def adapter(tmp_path, runner):
    return CodexTimelineSummaryAdapter(tmp_path / "provider", process_runner=runner)


def summarize(provider, **changes):
    return provider.summarize(**{
        "quest_ref": "quest-1", "nodes": nodes(), "native_session_ref": None,
        "job_ref": "recorder:quest-1:revision-1", **changes,
    })


def test_summary_uses_complete_basis_skill_schema_and_inherited_tool_isolation(tmp_path):
    runner = RecorderRunner()
    provider = adapter(tmp_path, runner)
    actual, session, next_scan = summarize(provider)
    assert actual == output()["summaries"]
    assert session == "recorder-native-1"
    assert next_scan == 900
    argv, prompt = runner.calls[0]
    assert "完整材料" * 600 in prompt
    assert "研究记录员" in prompt and "逐一核对" in prompt
    assert "next_scan_seconds" in prompt and "长实验" in prompt
    assert "不为调整间隔单独请求模型调用" in prompt
    assert "--ephemeral" not in argv
    assert 'mcp_servers={}' in argv and 'web_search="disabled"' in argv
    assert "shell_tool" in argv and "unified_exec" in argv
    assert argv[argv.index("--model") + 1] == "gpt-6-sol"
    assert 'model_reasoning_effort="max"' in argv
    schema = runner.schemas[0]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["summaries", "next_scan_seconds"]
    assert schema["properties"]["next_scan_seconds"] == {"type": "integer", "minimum": 300, "maximum": 7200}
    assert schema["properties"]["summaries"]["minItems"] == 2
    assert schema["properties"]["summaries"]["items"]["properties"]["summary"]["maxLength"] == 180


def test_summary_resumes_only_the_native_session_supplied_by_service(tmp_path):
    runner = RecorderRunner()
    provider = adapter(tmp_path, runner)
    _first, session, _interval = summarize(provider)
    second, resumed, interval = summarize(provider, native_session_ref=session, job_ref="recorder:quest-1:revision-2")
    assert resumed == session and second == output()["summaries"]
    assert interval == 900
    assert runner.calls[1][0][-3:] == ["resume", session, "-"]


def test_completed_job_replay_after_adapter_restart_does_not_call_provider(tmp_path):
    first_runner = RecorderRunner()
    expected = summarize(adapter(tmp_path, first_runner))

    class NoReplay(RecorderRunner):
        def __call__(self, *args):
            raise AssertionError("Completed operation must not run again")

        def run_command(self, *args):
            raise AssertionError("Completed replay must not probe the CLI")

    restarted = adapter(tmp_path, NoReplay())
    assert summarize(restarted) == expected
    assert restarted.reconcile_job("recorder:quest-1:revision-1") == "terminal"


def test_model_order_cannot_reorder_host_nodes(tmp_path):
    value = output()
    value["summaries"].reverse()
    assert summarize(adapter(tmp_path, RecorderRunner(value)))[0] == output()["summaries"]


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(extra="unexpected"),
    lambda value: value["summaries"].pop(),
    lambda value: value["summaries"].append(deepcopy(value["summaries"][0])),
    lambda value: value["summaries"][1].update(node_key="question:question-1"),
    lambda value: value["summaries"][1].update(node_key="target:another-quest"),
    lambda value: value["summaries"][1].update(source_hash="c" * 64),
    lambda value: value["summaries"][1].update(source_refs=["question-1"]),
    lambda value: value["summaries"][1].update(source_refs=["invented-result"]),
    lambda value: value["summaries"][1].update(source_refs=[]),
    lambda value: value["summaries"][1].update(source_refs=["progress-1", "progress-1"]),
    lambda value: value["summaries"][1].update(summary="  "),
    lambda value: value["summaries"][1].update(summary="第一行\n第二行"),
    lambda value: value["summaries"][1].update(summary="界" * 181),
    lambda value: value["summaries"][1].update(extra="unexpected"),
])
def test_summary_rejects_wrong_identity_basis_sources_or_shape(tmp_path, mutation):
    value = output()
    mutation(value)
    with pytest.raises(DraftingUnavailable, match="timeline_summary_output_invalid") as error:
        summarize(adapter(tmp_path, RecorderRunner(value)))
    assert error.value.native_session_ref == "recorder-native-1"


def test_english_scientific_sentence_may_exceed_chinese_character_limit(tmp_path):
    value = output()
    sentence = (
        "The subject-level split audit checks whether training and test partitions share participants, "
        "while the available evidence still leaves cross-subject generalization unresolved within the evaluated EEG dataset."
    )
    assert 180 < len(sentence) <= 360
    value["summaries"][1]["summary"] = sentence
    actual, _, _interval = summarize(adapter(tmp_path, RecorderRunner(value)), output_language="en")
    assert actual[1]["summary"] == sentence


def test_scientific_negative_results_are_not_rejected_by_keyword_filters(tmp_path):
    value = output()
    value["summaries"][1]["summary"] = "实验失败且统计检验未显著，当前不能证明模型有效。"
    assert summarize(adapter(tmp_path, RecorderRunner(value)))[0][1]["summary"] == value["summaries"][1]["summary"]


def test_node_without_sources_has_explicit_empty_citations(tmp_path):
    basis = nodes()
    basis[1]["sources"] = []
    value = output()
    value["summaries"][1]["source_refs"] = []
    assert summarize(adapter(tmp_path, RecorderRunner(value)), nodes=basis)[0][1]["source_refs"] == []


def test_duplicate_request_identity_fails_before_provider(tmp_path):
    runner = RecorderRunner()
    basis = nodes()
    basis[1]["node_key"] = basis[0]["node_key"]
    with pytest.raises(DraftingUnavailable, match="timeline_summary_request_invalid"):
        summarize(adapter(tmp_path, runner), nodes=basis)
    assert runner.calls == [] and runner.version_calls == 0


def test_changed_basis_cannot_reuse_a_completed_job_identity(tmp_path):
    runner = RecorderRunner()
    provider = adapter(tmp_path, runner)
    summarize(provider)
    basis = nodes()
    basis[1]["source_hash"] = "c" * 64
    with pytest.raises(DraftingUnavailable, match="codex_execution_contract_outdated|codex_job_spool_conflict"):
        summarize(provider, nodes=basis)
    assert len(runner.calls) == 1


@pytest.mark.parametrize("interval", [300, 600, 3600, 7200])
def test_valid_model_scan_interval_is_returned_with_the_same_summary_call(tmp_path, interval):
    value = output()
    value["next_scan_seconds"] = interval
    runner = RecorderRunner(value)
    summaries, session, actual = summarize(adapter(tmp_path, runner))
    assert summaries == output()["summaries"] and session == "recorder-native-1"
    assert actual == interval
    assert len(runner.calls) == 1


@pytest.mark.parametrize("interval", [None, True, False, "900", 900.0, 299, 7201, -1, {}, []])
def test_invalid_scan_interval_falls_back_without_discarding_valid_summaries(tmp_path, interval):
    value = output()
    value["next_scan_seconds"] = interval
    actual, _session, next_scan = summarize(adapter(tmp_path, RecorderRunner(value)))
    assert actual == output()["summaries"] and next_scan == 300


def test_missing_scan_interval_in_saved_output_uses_default(tmp_path):
    value = output()
    value.pop("next_scan_seconds")
    actual, _session, next_scan = summarize(adapter(tmp_path, RecorderRunner(value)))
    assert actual == value["summaries"] and next_scan == 300


def recovery_job():
    return {"quest_ref": "quest-1", "nodes": nodes(), "native_session_ref": None,
            "job_ref": "recorder:quest-1:revision-1", "output_language": "zh"}


class NoRecoveryInvocation(RecorderRunner):
    def __call__(self, *args):
        raise AssertionError("Recovery must not invoke the model")

    def run_command(self, *args):
        raise AssertionError("Recovery must not probe the CLI")


def test_completed_recovery_returns_scan_interval_without_a_model_call(tmp_path):
    expected = summarize(adapter(tmp_path, RecorderRunner()))
    recovered = adapter(tmp_path, NoRecoveryInvocation())
    assert recovered.reconcile_job(recovery_job()["job_ref"]) == "terminal"
    assert recovered.recover_summaries(recovery_job()) == expected


def test_old_prompt_and_schema_recover_without_requiring_the_new_schedule_field(tmp_path, monkeypatch):
    job = recovery_job()
    basis = {key: job[key] for key in ("quest_ref", "output_language", "nodes")}
    old_schema = _output_schema(len(job["nodes"]), 180)
    old_schema["properties"].pop("next_scan_seconds")
    old_schema["required"] = ["summaries"]
    value = output()
    value.pop("next_scan_seconds")
    first = adapter(tmp_path, RecorderRunner(value))
    first._invoke("Previous recorder instructions.\n\n" + json.dumps(basis, ensure_ascii=False), old_schema,
                  native_session_ref=None, ephemeral=False, job_ref=job["job_ref"])
    monkeypatch.setattr("meta_research.timeline_summary_provider._SKILL_PATH", tmp_path / "new-skill-unavailable.md")
    recovered = adapter(tmp_path, NoRecoveryInvocation())
    assert recovered.reconcile_job(job["job_ref"]) == "terminal"
    assert recovered.recover_summaries(job) == (value["summaries"], "recorder-native-1", 300)


@pytest.mark.parametrize("mutation", [
    lambda job: job.update(quest_ref="another-quest"),
    lambda job: job.update(native_session_ref="another-native-session"),
    lambda job: job["nodes"][0].update(content={"unknown_statement": "different research"}),
    lambda job: job.update(output_language="en"),
])
def test_recovery_keeps_original_quest_basis_language_and_session_bindings(tmp_path, mutation):
    summarize(adapter(tmp_path, RecorderRunner()))
    job = recovery_job()
    mutation(job)
    with pytest.raises(DraftingUnavailable, match="codex_job_spool_conflict"):
        adapter(tmp_path, NoRecoveryInvocation()).recover_summaries(job)

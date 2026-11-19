from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.owners.common import OwnerConflict
from meta_research.paths import DataRoot
from meta_research.quest_drafting import CODEX_DRAFTING_LOCKED_VERSION, DraftingUnavailable
from meta_research.recorder_sources import RecorderChannels, READ_SOURCE
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.timeline_summary_provider import CodexTimelineSummaryAdapter

Q = "question:question-1"
STAGE = "stage:cycle-1:reasoning"
CLAIM = {
    "stage": "reasoning",
    "status": "accepted",
    "source": {"outcome_ref": "judgment"},
    "content": {"claim": "当前数据不支持跨被试泛化。", "limitations": "缺外部队列"},
}
QUESTION = {"details": "长材料" * 1800, "limit": "尚未验证外部队列"}


def node(key, kind, refs):
    binding = {"judgment": {"kind": "artifact", "stage": "reasoning", "cycle_ref": "cycle-1",
                            "disposition": "completed", "commit_ref": "reasoning-commit", "outcome_ref": "judgment"},
                STAGE: {"kind": "stage", "stage": "reasoning", "cycle_ref": "cycle-1",
                        "artifacts": ["judgment"], "current": None}}
    return {
        "quest_ref": "quest-1",
        "question_ref": "question-1",
        "cycle_ref": "cycle-1",
        "stage": "reasoning" if kind == "stage" else None,
        "node_key": key,
        "kind": kind,
        "source_hash": ("a" if key == Q else "b") * 64,
        "sources": [{"ref": ref, "label": ref, "binding": {"version": 1, **binding.get(ref, {"kind": "identity"})}} for ref in refs],
    }


def job():
    return {
        "quest_ref": "quest-1",
        "job_ref": "recorder-job",
        "native_session_ref": None,
        "observed_at": 120,
        "output_language": "zh",
        "nodes": [node(Q, "question", ["question-1"]), node(STAGE, "stage", [STAGE, "cycle-1", "judgment"])],
    }


def output():
    return {
        "next_scan_seconds": 900,
        "summaries": [
            {
                "node_key": Q,
                "source_hash": "a" * 64,
                "summary": "核查脑电分类能否跨被试泛化，外部队列仍未验证。",
                "summary_kind": "process",
                "milestone": False,
                "source_refs": ["question-1"],
            },
            {
                "node_key": STAGE,
                "source_hash": "b" * 64,
                "summary": "现有队列的分类结果不支持跨被试泛化，外部队列仍待验证。",
                "summary_kind": "accepted_conclusion",
                "milestone": False,
                "source_refs": ["judgment"],
            },
        ],
    }


class Sources:
    def __init__(self):
        self.data = {
            "question-1": QUESTION,
            STAGE: {"stage": "reasoning", "artifacts": ["judgment"], "current": None},
            "cycle-1": {"cycle_ref": "cycle-1"},
            "judgment": CLAIM,
        }

    def read_source(self, quest, node, ref):
        assert quest == node["quest_ref"] == "quest-1"
        body = self.data[ref]
        if body is None:
            raise OwnerConflict("content_temporarily_unavailable")
        return body


class RecorderRunner:
    def __init__(self, value=None):
        self.value = output() if value is None else value
        self.sources = Sources()
        self.channels = RecorderChannels()
        self.channels.reader = self.sources
        self.channels.url = "http://127.0.0.1:18770/mcp"
        self.channels.gateway = SemanticMcpGateway((self.channels.operation,))
        self.calls = []
        self.reads = [(Q, "question-1"), (STAGE, "judgment")]
        self.token = None

    def run_command(self, argv, timeout):
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"codex-cli {CODEX_DRAFTING_LOCKED_VERSION}\n", stderr=""
        )

    def call(self, op, arguments):
        status, response = self.channels.gateway.dispatch(
            self.token,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": op, "arguments": arguments}},
        )
        return status, response

    def __call__(self, argv, prompt, timeout, environment):
        self.calls.append((argv, prompt))
        self.token = environment["META_RESEARCH_MCP_TOKEN"]
        events = [{"type": "thread.started", "thread_id": "native-recorder"}]
        for key, ref in self.reads:
            offset = 0
            while offset is not None:
                args = {"node_key": key, "source_ref": ref, "offset": offset}
                status, response = self.call(READ_SOURCE, args)
                assert status == 200
                result = response["result"]
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "server": "meta_research",
                            "tool": READ_SOURCE,
                            "arguments": args,
                            "status": "completed",
                            "result": result,
                        },
                    }
                )
                if result.get("isError"):
                    break
                offset = result["structuredContent"]["next_offset"]
        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(self.value))
        return subprocess.CompletedProcess(argv, 0, stdout="\n".join(map(json.dumps, events)), stderr="")


def adapter(tmp_path, runner):
    return CodexTimelineSummaryAdapter(tmp_path / "provider", process_runner=runner, channels=runner.channels)


def test_recorder_factory_reads_managed_stage_home_and_inherits_credential_environment(tmp_path, monkeypatch):
    from meta_research.timeline_summaries import create_timeline_summary_service

    root = DataRoot(tmp_path / "data")
    root.codex_home.mkdir(parents=True)
    runtime = SimpleNamespace(data_root=root, recorder_channels=RecorderRunner().channels)
    monkeypatch.setattr("meta_research.timeline_summary_sources.TimelineSummarySourceReader", lambda runtime: object())
    monkeypatch.setenv("TEST_RESEARCH_KEY", "test-secret-value")
    service = create_timeline_summary_service(runtime, "codex")
    provider = service._provider_factory()
    args = (tmp_path / "schema.json", tmp_path / "result.json")
    default = provider._provider_argv(*args, native_session_ref=None, ephemeral=False)
    assert not any(arg.startswith("model_provider=") for arg in default)
    (root.codex_home / "config.toml").write_text(
        'profile="deployment"\n[profiles.deployment]\nmodel_provider="gateway"\n'
        '[model_providers.gateway]\nname="Gateway"\nenv_key="TEST_RESEARCH_KEY"\n', encoding="utf-8")
    argv = provider._provider_argv(*args, native_session_ref=None, ephemeral=False)
    assert 'model_provider="gateway"' in argv and 'model_providers.gateway.env_key="TEST_RESEARCH_KEY"' in argv
    assert "test-secret-value" not in repr(argv)
    invocation_env = provider._process_runner._subprocess_environment({"META_RESEARCH_MCP_TOKEN": "grant"})
    assert invocation_env["TEST_RESEARCH_KEY"] == "test-secret-value"
    assert invocation_env["CODEX_HOME"] == str(root.codex_home)


def test_native_recorder_uses_only_selected_stage_provider_route_on_start_and_resume(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        'model_provider = "research-gateway"\nmodel = "other-model"\n'
        '[model_providers.research-gateway]\nname = "Research Gateway"\n'
        'base_url = "https://example.test/v1"\nwire_api = "responses"\nenv_key = "TEST_RESEARCH_KEY"\n'
        '[model_providers.unused]\nexperimental_bearer_token = "private-marker"\n'
        '[mcp_servers.extra]\nurl = "https://unrelated.test/mcp"\n', encoding="utf-8")
    monkeypatch.setenv("TEST_RESEARCH_KEY", "test-secret-value")
    runner = RecorderRunner()
    provider = CodexTimelineSummaryAdapter(tmp_path / "provider", process_runner=runner,
                                           channels=runner.channels, stage_provider_config=config)
    _, native, _ = provider.summarize(**job())
    provider.summarize(**dict(job(), job_ref="next-observation", native_session_ref=native))
    for argv, prompt in runner.calls:
        assert "--ignore-user-config" in argv and "--ignore-rules" in argv
        assert argv[argv.index("--model") + 1] == CODEX_MODEL_REF
        configs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--config"]
        route = tomllib.loads("\n".join(c for c in configs if c.startswith("model_provider")))
        assert route == {
            "model_provider": "research-gateway",
            "model_providers": {"research-gateway": {
                "name": "Research Gateway", "base_url": "https://example.test/v1",
                "wire_api": "responses", "env_key": "TEST_RESEARCH_KEY"}},
        }
        assert f'mcp_servers.meta_research.url="{runner.channels.url}"' in configs
        assert all(value not in repr((argv, prompt)) for value in
                   ("test-secret-value", "private-marker", "other-model", "unrelated.test"))


@pytest.mark.parametrize("bad_field", [
    'experimental_bearer_token = "private-marker"',
    'base_url = "https://user:private-marker@example.test/v1"',
    'base_url = "https://example.test/v1?token=private-marker"',
    'env_key = "private-marker!"',
])
def test_recorder_rejects_unsafe_selected_definition_before_provider_dispatch(tmp_path, bad_field):
    config = tmp_path / "config.toml"
    config.write_text('model_provider="gateway"\n[model_providers.gateway]\n' + bad_field, encoding="utf-8")
    runner = RecorderRunner()
    provider = CodexTimelineSummaryAdapter(tmp_path / "provider", process_runner=runner,
                                           channels=runner.channels, stage_provider_config=config)
    with pytest.raises(DraftingUnavailable, match="^timeline_summary_provider_config_invalid$"):
        provider.summarize(**job())
    assert runner.calls == []
    assert not provider._durable_job_directory(job()["job_ref"]).exists()


class ObservedNodes:
    def __init__(self):
        self.nodes = job()["nodes"]

    def quest_refs(self):
        return ["quest-1"]

    def read(self, _quest_ref):
        return deepcopy(self.nodes)


class CorrectingRunner(RecorderRunner):
    def __call__(self, argv, prompt, timeout, environment):
        request = json.loads(prompt.rsplit("\n", 1)[1])
        templates = {row["node_key"]: row for row in output()["summaries"]}
        self.value = {"next_scan_seconds": 900, "summaries": [
            dict(templates[n["node_key"]], source_hash=n["source_hash"]) for n in request["nodes"]]}
        if not self.calls:
            self.value["summaries"][0]["source_refs"] = ["judgment"]
        self.reads = []
        return super().__call__(argv, prompt, timeout, environment)


def test_grant_has_only_frozen_sources_and_long_native_reads_all_pages(tmp_path):
    runner = RecorderRunner()
    runner.reads += [(STAGE, "question-1"), (Q, "unknown"), ("another-quest", "question-1")]
    provider = adapter(tmp_path, runner)
    # A denied lookup cannot defeat valid source use by the same job.
    actual, native, interval = provider.summarize(**job())
    assert (actual, native, interval) == (output()["summaries"], "native-recorder", 900)
    argv, prompt = runner.calls[0]
    assert READ_SOURCE in prompt and "question-1" in prompt and "长材料" not in prompt
    assert "binding" not in prompt and runner.token not in repr((argv, prompt))
    assert all(
        ["--disable", feature] in [argv[i : i + 2] for i in range(len(argv))]
        for feature in ("multi_agent", "shell_tool", "unified_exec", "view_image")
    )
    assert runner.call(READ_SOURCE, {"node_key": Q, "source_ref": "question-1"})[0] == 401
    with runner.channels.grant("other", job(), None) as access:
        runner.token = access.token
        for operation in ("agent_runtime.target.launch", "shell", "research_memory.accept", "spawn_agent"):
            assert runner.call(operation, {})[1]["result"]["isError"]
        for key, ref in ((Q, "judgment"), (STAGE, "question-1"), ("foreign", "judgment")):
            assert runner.call(READ_SOURCE, {"node_key": key, "source_ref": ref})[1]["result"]["isError"]


def test_native_resume_receives_only_changes_and_restart_uses_original_job(tmp_path):
    runner = RecorderRunner()
    provider = adapter(tmp_path, runner)
    request = job()
    result = provider.summarize(**request)
    restarted = adapter(tmp_path, RecorderRunner())
    assert restarted.recover_summaries(request) == result
    second = dict(request, job_ref="second", native_session_ref=result[1], observed_at=500)
    provider.summarize(**second)
    assert runner.calls[1][0][-3:] == ["resume", "native-recorder", "-"]
    assert "长材料" not in runner.calls[1][1] and len(runner.calls[1][1]) < len(runner.calls[0][1])
    altered = deepcopy(request)
    altered["nodes"][0]["sources"][0]["binding"]["version"] = 2
    with pytest.raises(DraftingUnavailable, match="conflict"):
        restarted.recover_summaries(altered)


def test_new_native_sessions_keep_the_same_recorder_workspace(tmp_path):
    runner = RecorderRunner()
    runner.reads = []
    for index in range(3):
        adapter(tmp_path, runner).summarize(**dict(job(), job_ref=f"new-session-{index}", native_session_ref=None))
    directories = [argv[argv.index("--cd") + 1] for argv, _ in runner.calls]
    assert len(set(directories)) == 1
    assert Path(directories[0]) == tmp_path / "provider" / "research-workspace"
    assert all("resume" not in argv for argv, _ in runner.calls)


def test_new_and_existing_recorder_receive_shared_principles_with_read_only_role(tmp_path):
    from meta_research.system_prompt import RESEARCH_SYSTEM_PROMPT

    runner = RecorderRunner()
    provider = adapter(tmp_path, runner)
    request = job()
    _, native, _ = provider.summarize(**request)
    provider.summarize(**dict(request, job_ref="next-observation", native_session_ref=native))

    for argv, prompt in runner.calls:
        configs = {}
        for i, arg in enumerate(argv[:-1]):
            if arg == "--config":
                configs.update(tomllib.loads(argv[i + 1]))
        developer = configs["developer_instructions"]
        assert RESEARCH_SYSTEM_PROMPT in developer
        assert "只读研究记录员" in developer and READ_SOURCE in developer
        role = Path(configs["model_instructions_file"]).read_text(encoding="utf-8")
        assert "上层说明下层工作合起来做了什么" in role
        assert "正式收下的结果也可能是否定或不确定" in role
        assert "cycle_position.cycle_focus" in role
        assert "每个 Cycle 节点的 `summary` 都必须以本轮重点开头" in role
        assert "可读性必须高，说人话" in role
        assert "每个 Cycle 的第一句先说明当前 cycle_focus" in prompt
        assert "非专业读者一遍读懂" in prompt
        assert RESEARCH_SYSTEM_PROMPT not in prompt  # Turn data remains the source delta.
        assert configs["agents"]["enabled"] is False
        assert set(configs["mcp_servers"]) == {"meta_research"}
    assert runner.calls[1][0][-3:] == ["resume", native, "-"]
    assert "当前记录员说明" in runner.calls[1][1]


@pytest.mark.parametrize("language,limit,explanation", [
    ("zh", 600,
     "这一轮先检查了脑电数据，逐个核对文件能否打开、每个人的记录是否完整，以及电极名称能否对应起来。"
     "随后把参与者分成开发组和确认组，开发组用于选择分析方法，确认组留到方法确定后再测试。"
     "研究人员比较了两种计算脑电参考信号的方式，观察它们是否影响模型对新参与者的预测。"
     "为了看清差别，还分别检查了完整电极和减少电极后的结果，并把各次运行的设置、预测和图表整理到一起。"
     "最后结合这些结果讨论了下一轮应该继续检查的问题，让后续工作能够接着这批数据和分析开展。"),
    ("en", 1800,
     "The team first checked whether the EEG files opened correctly and whether each participant had complete recordings. "
     "They separated participants into a development group for choosing the analysis method and a confirmation group for testing it afterward. "
     "They then compared two ways of calculating the reference signal and checked how each affected predictions for new participants. "
     "The saved settings, predictions and charts show what was done and provide a starting point for the next round of work."),
])
def test_readable_multi_sentence_explanation_can_exceed_old_length_limit(tmp_path, language, limit, explanation):
    value = output()
    value["summaries"][0]["summary"] = explanation
    runner = RecorderRunner(value)
    runner.sources.data["question-1"] = {"work_record": explanation}
    request = dict(job(), output_language=language)
    assert len(explanation) > {"zh": 180, "en": 360}[language]
    rows, _, _ = adapter(tmp_path, runner).summarize(**request)
    assert rows[0]["summary"] == explanation
    argv, _ = runner.calls[0]
    schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    assert schema["properties"]["summaries"]["items"]["properties"]["summary"]["maxLength"] == limit

    runner.value["summaries"][0]["summary"] = "x" * (limit + 1)
    with pytest.raises(DraftingUnavailable, match="output_invalid"):
        adapter(tmp_path / "too-long", runner).summarize(**request)


def test_empty_stage_sentence_uses_frozen_stage_metadata_without_tool_reads(tmp_path):
    runner = RecorderRunner()
    request = job()
    request["nodes"][1]["sources"][0]["binding"]["artifacts"] = []
    runner.value["summaries"][1].update(summary_kind="process", summary="可能正在执行。", source_refs=[STAGE])
    runner.reads = []
    rows, _, _ = adapter(tmp_path, runner).summarize(**request)
    assert rows[1]["summary"] == "暂时没有这个阶段的工作记录。"
    assert rows[1]["summary_kind"] == "insufficient_evidence" and rows[1]["source_refs"] == ["cycle-1"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v["summaries"].pop(),
        lambda v: v["summaries"][0].update(source_hash="wrong"),
        lambda v: v["summaries"][0].update(source_refs=["judgment"]),
        lambda v: v["summaries"][0].update(summary_kind="accepted_conclusion"),
        lambda v: v["summaries"][0].update(milestone=True),
        lambda v: v["summaries"][0].pop("summary_kind"),
        lambda v: v["summaries"][0].update(summary="first\nsecond"),
        lambda v: v["summaries"][0].update(milestone="true"),
        lambda v: v["summaries"][1].update(source_refs=[STAGE]),
    ],
)
def test_output_must_match_requested_nodes_allowed_references_and_schema(tmp_path, mutate):
    value = output()
    mutate(value)
    with pytest.raises(DraftingUnavailable, match="output_invalid"):
        adapter(tmp_path, RecorderRunner(value)).summarize(**job())


@pytest.mark.parametrize("interval", [300, 600, 3600, 7200, True, 7201])
def test_interval_travels_with_valid_summary_and_negative_results_are_preserved(tmp_path, interval):
    value = output()
    value["next_scan_seconds"] = interval
    sentence = "复核得到零增益，两次负结果未支持假设，另一次采集失败没有测量值。"
    value["summaries"][0]["summary"] = sentence
    runner = RecorderRunner(value)
    runner.sources.data["question-1"] = {"public_record": sentence}
    result, _, seconds = adapter(tmp_path, runner).summarize(**job())
    assert result[0]["summary"] == sentence
    assert seconds == (interval if type(interval) is int and interval <= 7200 else 300)
    assert len(runner.calls) == 1


def test_isolated_cycle_reads_child_judgment_and_its_limits(tmp_path):
    request = job()
    cycle = node("cycle:cycle-1", "cycle", ["judgment"])
    request["nodes"] = [cycle]
    value = output()
    value["summaries"] = [dict(value["summaries"][1], node_key=cycle["node_key"])]
    runner = RecorderRunner(value)
    runner.reads = [(cycle["node_key"], "judgment")]
    assert adapter(tmp_path, runner).summarize(**request)[0] == value["summaries"]


@pytest.mark.parametrize("change", [
    {"disposition": "skipped"}, {"commit_ref": None}, {"outcome_ref": None},
    {"cycle_ref": "another-cycle"}, {"stage": "bundle"},
])
def test_accepted_conclusion_requires_this_cycles_committed_reasoning_identity(tmp_path, change):
    runner = RecorderRunner()
    runner.reads = []
    request = job()
    request["nodes"][1]["sources"][-1]["binding"].update(change)
    with pytest.raises(DraftingUnavailable, match="output_invalid"):
        adapter(tmp_path, runner).summarize(**request)


def test_model_order_does_not_reorder_requested_nodes(tmp_path):
    value = output()
    value["summaries"].reverse()
    assert adapter(tmp_path, RecorderRunner(value)).summarize(**job())[0] == output()["summaries"]


def test_research_text_with_claim_shaped_fields_cannot_impersonate_owner_artifact(tmp_path):
    request = job()
    request["nodes"][1]["sources"][-1]["binding"]["kind"] = "note"
    with pytest.raises(DraftingUnavailable, match="output_invalid"):
        adapter(tmp_path, RecorderRunner()).summarize(**request)


def test_summary_can_use_allowed_sources_without_current_tool_reads(tmp_path):
    runner = RecorderRunner()
    runner.reads = []
    assert adapter(tmp_path, runner).summarize(**job()) == (output()["summaries"], "native-recorder", 900)


def test_optional_source_read_failure_does_not_gate_valid_summary_output(tmp_path):
    runner = RecorderRunner()
    runner.sources.data["question-1"] = None
    assert adapter(tmp_path, runner).summarize(**job())[0] == output()["summaries"]


@pytest.mark.parametrize("problem,code", [
    ("missing", "missing_node"), ("duplicate", "duplicate_node"), ("unknown", "missing_node"),
])
def test_malformed_node_identity_does_not_discard_uniquely_identified_results(tmp_path, problem, code):
    value = output()
    if problem == "missing":
        value["summaries"].pop(0)
    elif problem == "duplicate":
        value["summaries"].append(dict(value["summaries"][0]))
    else:
        value["summaries"][0]["node_key"] = "question:another-question"
    runner = RecorderRunner(value)
    runner.reads = []
    with pytest.raises(DraftingUnavailable, match="output_invalid") as failed:
        adapter(tmp_path, runner).summarize(**job())
    assert failed.value.summaries == [output()["summaries"][1]]
    assert set(failed.value.failures) == {Q}
    assert failed.value.failures[Q]["code"] == code


def test_restart_recovers_partial_results_without_another_model_call(tmp_path):
    value = output()
    value["summaries"][0]["source_refs"] = ["judgment"]
    runner = RecorderRunner(value)
    runner.reads = []
    with pytest.raises(DraftingUnavailable, match="output_invalid") as original:
        adapter(tmp_path, runner).summarize(**job())

    restarted_runner = RecorderRunner()
    restarted = adapter(tmp_path, restarted_runner)
    with pytest.raises(DraftingUnavailable, match="output_invalid") as recovered:
        restarted.recover_summaries(job())
    assert recovered.value.summaries == original.value.summaries == [output()["summaries"][1]]
    assert recovered.value.failures == original.value.failures
    assert recovered.value.native_session_ref == "native-recorder"
    assert recovered.value.next_scan_seconds == 900
    assert not restarted_runner.calls


def test_recorder_restart_corrects_only_invalid_node_with_feedback(tmp_path):
    from meta_research.timeline_summaries import TimelineSummaryService, TimelineSummaryStore

    runner = CorrectingRunner()
    database = tmp_path / "summaries.sqlite3"
    first = TimelineSummaryService(TimelineSummaryStore(database), ObservedNodes(),
        lambda: adapter(tmp_path, runner), clock=lambda: 100)
    assert first.process_once()
    rows = {row["node_key"]: row for row in first.query("quest-1")["nodes"]}
    assert rows[STAGE]["status"] == "ready" and rows[STAGE]["summary"] == output()["summaries"][1]["summary"]
    assert rows[Q]["status"] == "failed" and rows[Q]["summary"] is None

    restarted = TimelineSummaryService(TimelineSummaryStore(database), ObservedNodes(),
        lambda: adapter(tmp_path, runner), clock=lambda: 101)
    assert restarted.process_once()
    assert all(row["status"] == "ready" for row in restarted.query("quest-1")["nodes"])
    assert len(runner.calls) == 2
    argv, prompt = runner.calls[1]
    request = json.loads(prompt.rsplit("\n", 1)[1])
    assert [n["node_key"] for n in request["nodes"]] == [Q]
    assert request["nodes"][0]["retry_feedback"]["source_refs"] == ["judgment"]
    assert request["nodes"][0]["retry_feedback"]["code"] == "unallowed_source_refs"
    assert argv[-3:] == ["resume", "native-recorder", "-"]
    assert "retry_feedback" in prompt


def test_partial_publication_recovers_after_restart_without_counting_another_call(tmp_path):
    from meta_research.timeline_summaries import TimelineSummaryService, TimelineSummaryStore

    class UnavailableStore(TimelineSummaryStore):
        def publish(self, *args, **kwargs):
            raise OSError("temporary storage failure")

    runner, reader, now = CorrectingRunner(), ObservedNodes(), [100]
    database = tmp_path / "summaries.sqlite3"
    first = TimelineSummaryService(UnavailableStore(database), reader,
        lambda: adapter(tmp_path, runner), clock=lambda: now[0])
    assert not first.process_once()
    assert len(runner.calls) == 1
    assert all(row["summary"] is None for row in first.query("quest-1")["nodes"])

    now[0] = 131
    restarted = TimelineSummaryService(TimelineSummaryStore(database), reader,
        lambda: adapter(tmp_path, runner), clock=lambda: now[0])
    assert restarted.process_once()
    assert len(runner.calls) == 1  # Recover the terminal spool; no second CLI invocation.
    rows = {row["node_key"]: row for row in restarted.query("quest-1")["nodes"]}
    assert rows[STAGE]["status"] == "ready" and rows[Q]["status"] == "failed"

    now[0] = 132
    assert restarted.process_once()
    assert len(runner.calls) == 2
    correction = json.loads(runner.calls[1][1].rsplit("\n", 1)[1])
    assert [n["node_key"] for n in correction["nodes"]] == [Q]
    assert all(row["status"] == "ready" for row in restarted.query("quest-1")["nodes"])

    # Publication recovery must not consume a model call: the next genuine
    # observation is the third call and still resumes this native session.
    reader.nodes[0]["source_hash"] = "next-observation"
    now[0] = 1100
    assert restarted.process_once()
    assert len(runner.calls) == 3
    assert runner.calls[2][0][-3:] == ["resume", "native-recorder", "-"]


def test_unrelated_root_cannot_discover_or_call_the_recorders_operation(tmp_path):
    runner = RecorderRunner()
    gateway = runner.channels.gateway
    # Even an accidentally over-broad operation list cannot borrow the recorder's active grant.
    unrelated, _ = gateway.issue_channel(run_ref="other", attempt_ref="a", root_session_ref="other",
        fence_ref="other", capability_binding_hash="a"*64, operation_ids=(READ_SOURCE,), root_kind=None,
        phase="other")
    try:
        with runner.channels.grant("recorder-job", job(), None):
            status, listed = gateway.dispatch(unrelated.token, {"jsonrpc":"2.0","method":"tools/list","id":1})
            assert status == 200 and listed["result"]["tools"] == []
            runner.token = unrelated.token
            result = runner.call(READ_SOURCE, {"node_key":Q,"source_ref":"question-1"})[1]["result"]
            assert result["isError"] and result["structuredContent"]["code"] == "capability_unavailable"
    finally:
        gateway.revoke_channel(unrelated.token)


def test_source_pages_pin_one_successful_read_for_this_binding_and_grant():
    runner = RecorderRunner()
    request = job()
    current = {"text": "a" * 194_980}
    owner_reads = 0
    request["nodes"][1]["sources"].append(
        {"ref": "question-1", "label": "another version", "binding": {"version": 2}}
    )

    def read(quest, node, ref):
        nonlocal owner_reads
        owner_reads += 1
        if owner_reads == 1:
            raise OwnerConflict("content_temporarily_unavailable")
        return current

    runner.sources.read_source = read

    def page(offset, key=Q):
        status, response = runner.call(READ_SOURCE, {"node_key": key, "source_ref": "question-1", "offset": offset})
        assert status == 200
        return response["result"]["structuredContent"]

    with runner.channels.grant("job-1", request, None) as access:
        runner.token = access.token
        assert page(0)["code"] == "content_temporarily_unavailable"
        first = page(0)
        current = {"text": "b" * 194_980}
        text, offset = first["text"], first["next_offset"]
        while offset is not None:
            chunk = page(offset)
            text += chunk["text"]
            offset = chunk["next_offset"]
        assert json.loads(text) == {"text": "a" * 194_980}
        assert owner_reads == 2  # one unavailable read, then one body for all 56 pages
        assert "bbbb" in page(0, key=STAGE)["text"]
        assert owner_reads == 3

    with runner.channels.grant("job-2", request, None) as access:
        runner.token = access.token
        assert "bbbb" in page(0)["text"]
        assert owner_reads == 4


def test_adapter_validates_shared_ref_against_each_nodes_exact_binding(tmp_path):
    cycle = node("cycle:cycle-1", "cycle", ["judgment"])
    cycle["sources"][0]["binding"].update(version=2, disposition="skipped")
    request = job()
    request["nodes"] = [node(STAGE, "stage", ["judgment"]), cycle]
    value = output()
    judgment = value["summaries"][1]
    progress = dict(judgment, node_key=cycle["node_key"], summary_kind="process", summary="正在核对新增队列的数据。")
    value["summaries"] = [judgment, progress]
    runner = RecorderRunner(value)
    runner.reads = []

    assert adapter(tmp_path, runner).summarize(**request)[0] == [judgment, progress]
    progress["summary_kind"] = "accepted_conclusion"
    with pytest.raises(DraftingUnavailable, match="output_invalid"):
        adapter(tmp_path / "wrong-version", runner).summarize(**request)


def test_recorder_releases_its_job_scope_even_if_gateway_cleanup_raises(monkeypatch):
    runner = RecorderRunner()

    def fail_cleanup(_token):
        raise RuntimeError("injected cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(runner.channels.gateway, "revoke_channel", fail_cleanup)
        with pytest.raises(RuntimeError, match="injected cleanup failure"):
            with runner.channels.grant("job-1", job(), None) as old:
                assert runner.channels.is_current(old.token)
    assert not runner.channels.is_current(old.token)
    with runner.channels.grant("job-2", job(), None) as new:
        assert runner.channels.is_current(new.token)

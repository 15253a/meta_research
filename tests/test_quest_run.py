"""One-command quest creation/reopen handoff to the canonical run owner."""
from pathlib import Path

from orchestrator import quest_run
from orchestrator.quest_registry import QuestRegistry


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def test_template_create_and_reopen_delegate_exact_isolated_work_root(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quest_run, "run_main", lambda argv: calls.append(argv) or 0)
    root = tmp_path / "registry"
    common = [
        "--system-root", str(SYSTEM_ROOT), "--quests-root", str(root),
        "--quest-id", "t1", "--max-cycles", "0", "--once", "--no-outbound",
    ]
    assert quest_run.main([
        *common, "--title", "T1", "--template-id", "t1-eeg-universal",
    ]) == 0
    quest = QuestRegistry(root, SYSTEM_ROOT).get("t1")
    assert calls[-1] == [
        "--system-root", str(SYSTEM_ROOT), "--work-root", str(quest.work_root),
        "--max-cycles", "0", "--poll-interval-s", "1.0", "--once", "--no-outbound",
    ]

    # Reopen needs no creation input and cannot create a second namespace.
    assert quest_run.main(common) == 0
    assert calls[-1][calls[-1].index("--work-root") + 1] == str(quest.work_root)
    assert [item.quest_id for item in QuestRegistry(root, SYSTEM_ROOT).list()] == ["t1"]


def test_missing_quest_fails_before_run_owner(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(quest_run, "run_main", lambda argv: calls.append(argv) or 0)
    assert quest_run.main([
        "--system-root", str(SYSTEM_ROOT),
        "--quests-root", str(tmp_path / "registry"),
        "--quest-id", "missing", "--no-outbound",
    ]) == 2
    assert calls == []
    assert "任务准备失败" in capsys.readouterr().err


def test_custom_goal_brief_symlink_fails_before_creation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quest_run, "run_main", lambda argv: calls.append(argv) or 0)
    source = tmp_path / "goal.md"
    source.write_text("---\npredicate_json: {kind: custom}\n---\n目标\n", encoding="utf-8")
    link = tmp_path / "goal-link.md"
    link.symlink_to(source)

    assert quest_run.main([
        "--system-root", str(SYSTEM_ROOT),
        "--quests-root", str(tmp_path / "registry"),
        "--quest-id", "unsafe", "--goal-brief", str(link), "--no-outbound",
    ]) == 2
    assert calls == []
    assert not (tmp_path / "registry" / "quests" / "unsafe").exists()

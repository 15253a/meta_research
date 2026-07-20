"""Create (or reopen) one isolated quest and start its sole research owner.

This is the acceptance-friendly one-command entry point.  The registry owns
task creation and physical isolation; ``orchestrator.run`` remains the only
research writer and keeps all of its deployment/lease checks.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import List, Optional

from .quest_registry import QuestConflictError, QuestCorruptError, QuestRegistry
from .run import main as run_main


_MAX_GOAL_BRIEF_BYTES = 256 * 1024


def _nonnegative(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("须为非负整数") from error
    if result < 0:
        raise argparse.ArgumentTypeError("须为非负整数")
    return result


def _read_goal_brief(path_value: str) -> str:
    path = Path(path_value)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"goal brief 不可读: {error}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("goal brief 须为非 symlink 常规文件")
        if not 0 < info.st_size <= _MAX_GOAL_BRIEF_BYTES:
            raise ValueError(
                f"goal brief 大小须在 1..{_MAX_GOAL_BRIEF_BYTES} bytes")
        chunks = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != info.st_size:
            raise ValueError("goal brief 读取期间尺寸变化")
        return body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("goal brief 须为 UTF-8") from error
    except OSError as error:
        raise ValueError(f"goal brief 读取失败: {error}") from error
    finally:
        os.close(fd)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="创建/恢复一个物理隔离 quest，并直接启动其唯一 run owner")
    parser.add_argument("--system-root", required=True)
    parser.add_argument("--quests-root", required=True)
    parser.add_argument("--quest-id", required=True)
    parser.add_argument("--title", help="新建时的展示名；默认等于 quest-id")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--template-id", help="从 <system-root>/quest_templates/<id> 创建")
    source.add_argument("--goal-brief", help="从本地 UTF-8 goal_brief.md 创建")
    parser.add_argument("--max-cycles", type=_nonnegative, default=100)
    lifetime = parser.add_mutually_exclusive_group()
    lifetime.add_argument("--once", action="store_true")
    lifetime.add_argument("--exit-after-research", action="store_true")
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    outbound = parser.add_mutually_exclusive_group(required=True)
    outbound.add_argument("--connector-profile")
    outbound.add_argument(
        "--no-outbound", action="store_true",
        help="显式承认本次只做本地/验收运行，不对外投递通知")
    args = parser.parse_args(argv)

    try:
        registry = QuestRegistry(Path(args.quests_root), Path(args.system_root))
        if args.template_id:
            quest = registry.create_from_template(
                quest_id=args.quest_id, title=args.title or args.quest_id,
                template_id=args.template_id)
        elif args.goal_brief:
            quest = registry.create(
                quest_id=args.quest_id, title=args.title or args.quest_id,
                goal_brief_md=_read_goal_brief(args.goal_brief))
        else:
            quest = registry.get(args.quest_id)
    except (KeyError, ValueError, QuestConflictError, QuestCorruptError, OSError) as error:
        print(f"[quest-run] 任务准备失败：{error}", file=sys.stderr)
        return 2

    print(
        f"[quest-run] {'已创建' if quest.created else '已恢复'} quest={quest.quest_id}；"
        f"work-root={quest.work_root}")
    run_argv = [
        "--system-root", str(Path(args.system_root)),
        "--work-root", str(quest.work_root),
        "--max-cycles", str(args.max_cycles),
        "--poll-interval-s", str(args.poll_interval_s),
    ]
    if args.once:
        run_argv.append("--once")
    elif args.exit_after_research:
        run_argv.append("--exit-after-research")
    if args.no_outbound:
        run_argv.append("--no-outbound")
    else:
        run_argv.extend(["--connector-profile", args.connector_profile])
    return run_main(run_argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""Zero-configuration local entry point for the Meta-Research Web product.

After installation a user runs ``python -m orchestrator.web_app``.  The
repository location is discovered from this package, persistent quest state
defaults to ``<system-root>/runtime``, and the authenticated Web page is opened
automatically.  All post-deployment task and file operations then stay inside
that page.
"""
from __future__ import annotations

import argparse
import os
import sysconfig
from pathlib import Path
from typing import List, Optional

from .console_server import main as console_main


_RUNTIME_DIRNAME = "runtime"


def _default_data_root(system_root: Path) -> Path:
    """Keep one installation's tasks beside that installation by default.

    ``META_RESEARCH_HOME`` remains an explicit deployment override for a
    read-only installation or a deliberately relocated workspace.  XDG is not
    consulted implicitly: a local product checkout should be self-contained
    and removable as one directory tree.
    """
    configured = os.environ.get("META_RESEARCH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(system_root) / _RUNTIME_DIRNAME


_SYSTEM_MARKERS = (
    "db/migrations/0001_appendix_a.sql",
    "engines/wildidea/UPSTREAM.json",
    "input/goal_brief.md",
    "policies/policy.yaml",
    "prompts/system_prompt.md",
    "schemas/policy.schema.json",
    "views/console/index.html",
)


def _is_system_root(path: Path) -> bool:
    return all((path / marker).is_file() for marker in _SYSTEM_MARKERS)


def _system_root() -> Path:
    """Resolve assets from a source checkout or a normal wheel installation."""
    configured = os.environ.get("META_RESEARCH_SYSTEM_ROOT")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    package_parent = Path(__file__).resolve().parent.parent
    candidates.extend([
        package_parent,
        package_parent / "share" / "meta-research",
        Path(sysconfig.get_path("data")) / "share" / "meta-research",
    ])
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if _is_system_root(resolved):
            return resolved
    raise RuntimeError(
        "Meta-Research 运行资产缺失；请重新安装软件包（模板、策略、schema 与 Web 页面必须一同安装）")


def main(argv: Optional[List[str]] = None) -> int:
    system_root = _system_root()
    parser = argparse.ArgumentParser(
        description="启动本机 Meta-Research Web 产品（任务、目录与运行控制均在页面内完成）")
    parser.add_argument(
        "--data-root", default=str(_default_data_root(system_root)),
        help="任务数据根目录（默认：<Meta-Research 安装目录>/runtime）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-cycles", type=int, default=100)
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    parser.add_argument("--connector-profile")
    parser.add_argument("--qualification-profiles-root")
    parser.add_argument("--local-import-root", action="append", default=None)
    parser.add_argument("--no-open-browser", action="store_true")
    args = parser.parse_args(argv)

    command = [
        "--system-root", str(system_root),
        "--quests-root", str(Path(args.data_root).expanduser()),
        "--host", args.host,
        "--port", str(args.port),
        "--max-cycles", str(args.max_cycles),
        "--poll-interval-s", str(args.poll_interval_s),
    ]
    if args.connector_profile:
        command.extend(["--connector-profile", args.connector_profile])
    else:
        command.append("--no-outbound")
    if args.qualification_profiles_root:
        command.extend([
            "--qualification-profiles-root",
            str(Path(args.qualification_profiles_root).expanduser()),
        ])
    for root in args.local_import_root or []:
        command.extend(["--local-import-root", str(Path(root).expanduser())])
    if args.no_open_browser:
        command.append("--no-open-browser")
    return console_main(command)


if __name__ == "__main__":
    raise SystemExit(main())

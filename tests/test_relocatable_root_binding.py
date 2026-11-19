"""Execution identity survives a real relocation of the installed package.

The full ``meta_research`` package is copied to two unrelated absolute install
prefixes on disk.  Two separate interpreters import their own copy through
``PYTHONPATH`` and build the real Codex adapters' runtime bindings; both must
produce one identity although ``module.__file__`` and every delivered skill
path differ.  A genuine byte change inside copy B (one guidance line carrying
a business absolute path) must still move the shared root profile.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import meta_research

from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.root_capabilities import ROOT_AGENT_KINDS

ADAPTERS = ("CodexIdeaSkillAdapter", "CodexPlanSkillAdapter",
            "CodexBundleSkillAdapter", "CodexReasoningSkillAdapter")

_PROBE = r'''
import json, re, sys
from pathlib import Path

workspace, package = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
import meta_research
resolved = Path(meta_research.__file__).resolve()
assert resolved == (package / "__init__.py").resolve(), resolved

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.owners.common import canonical_hash
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.research_guidance import CYCLE_ROLE_DIRECTORIES
from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_capability_profile

bindings = {
    cls.__name__: canonical_hash(
        cls(workspace, executable=sys.executable, model_ref=sys.argv[3])
        .runtime_binding().as_dict()
    )
    for cls in (CodexIdeaSkillAdapter, CodexPlanSkillAdapter,
                CodexBundleSkillAdapter, CodexReasoningSkillAdapter)
}
profiles = {}
for kind in ROOT_AGENT_KINDS:
    profile = root_capability_profile(kind, sys.argv[3])
    profiles[kind] = profile.digest
    argv = profile.codex_arguments()
    developer = next(json.loads(arg[len("developer_instructions="):])
                     for arg in argv
                     if arg.startswith("developer_instructions="))
    root = Path(re.search(r"Shared reference root: (.+)", developer).group(1))
    assert root.is_dir() and root.resolve().is_relative_to(resolved.parent), root
    if kind in CYCLE_ROLE_DIRECTORIES:
        skill = Path(re.search(r"Role skill source: (.+)", developer).group(1))
        assert skill.is_file() and skill.resolve().is_relative_to(resolved.parent)
print("PROBE:" + json.dumps(
    {"module": str(resolved), "bindings": bindings, "profiles": profiles}))
'''


def _probe(package: Path, workspace: Path, script: Path) -> dict:
    env = {**os.environ, "PYTHONPATH": str(package.parent),
           "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, str(script), str(workspace), str(package), CODEX_MODEL_REF],
        cwd=str(workspace), env=env, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(next(line for line in done.stdout.splitlines()
                           if line.startswith("PROBE:"))[len("PROBE:"):])


def test_relocated_installations_bind_one_identity(tmp_path):
    source = Path(meta_research.__file__).resolve().parent
    installed = [tmp_path / "opt" / "release-a" / "meta_research",
                 tmp_path / "srv" / "other-host" / "release-b" / "meta_research"]
    for target in installed:
        shutil.copytree(source, target,
                        ignore=shutil.ignore_patterns("__pycache__"))
    # Two independent workspaces share one already-existing transport seal and
    # the same fixed model, so only the installation prefix differs.
    seal_value = b"relocatable-root-binding-seal-01"
    workspaces = [tmp_path / "workspace-a", tmp_path / "workspace-b"]
    for workspace in workspaces:
        seal = workspace / "provider-operations" / ".transport-seal.key"
        seal.parent.mkdir(parents=True)
        seal.write_bytes(seal_value)
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")

    first = _probe(installed[0], workspaces[0], script)
    second = _probe(installed[1], workspaces[1], script)

    # Real imports of distinct copies; one shared execution identity.
    assert first["module"] != second["module"]
    assert set(first["bindings"]) == set(ADAPTERS) == set(second["bindings"])
    assert first["bindings"] == second["bindings"]
    assert set(first["profiles"]) == set(ROOT_AGENT_KINDS)
    assert first["profiles"] == second["profiles"]

    # A substantive guidance change carrying a business absolute path in B.
    guidance = installed[1] / "skills" / "references" / "research-guidance.md"
    business = tmp_path / "business" / "material-2026"
    guidance.write_text(
        guidance.read_text(encoding="utf-8")
        + f"\n业务材料固定位于 {business.as_posix()}，读取前先核对实际目录。\n",
        encoding="utf-8",
    )
    changed = _probe(installed[1], workspaces[1], script)
    assert changed["bindings"] != second["bindings"]
    assert all(changed["profiles"][kind] != second["profiles"][kind]
               for kind in ROOT_AGENT_KINDS)

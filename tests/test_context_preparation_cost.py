"""Measure real preparation I/O and observation work at the Plan adapter seam.

The probe uses only interfaces present on both the baseline and implementation,
so it can also be copied beside either checkout for before/after measurements.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import logging
from pathlib import Path
import platform
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from meta_research import context_presentation, idea_skill
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.owners.common import canonical_json
from test_plan_skill_adapter import _fake_codex, _plan, _request, _SequenceRunner


class _ReadCounter:
    def __init__(self, stream, counts):
        self.stream, self.counts = stream, counts

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, *args):
        value = self.stream.read(*args)
        self.counts["bytes"] += len(value.encode("utf-8") if isinstance(value, str) else value)
        return value


class _HashCounter:
    def __init__(self, digest, counts):
        self.digest, self.counts = digest, counts

    def __getattr__(self, name):
        return getattr(self.digest, name)

    def update(self, value):
        self.counts["bytes"] += len(value)
        self.digest.update(value)


def measure_plan_preparation(directory: Path, level: int):
    """Observe binding -> generate without substituting either preparation."""
    directory.mkdir(parents=True)
    runner = _SequenceRunner([{"plan": _plan()}])
    adapter = CodexPlanSkillAdapter(directory / "provider",
        executable=str(_fake_codex(directory / "codex")), process_runner=runner)
    package_root = Path(idea_skill.__file__).resolve().parent
    counters = {"binding": {"resources": {}, "source_hashes": {}},
                "generate": {"resources": {}, "source_hashes": {}}}
    observation = {"json_calls": 0, "json_bytes": 0, "sha256_calls": 0, "sha256_bytes": 0}
    phase, observing = "binding", False
    original_open, original_sha256 = Path.open, hashlib.sha256
    original_json, original_observe = context_presentation._json, context_presentation._observe_context_projection

    def entry(kind, name):
        return counters[phase][kind].setdefault(name, {"calls": 0, "bytes": 0})

    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        resolved = path.resolve()
        mode = args[0] if args else kwargs.get("mode", "r")
        if ("r" in mode and resolved.is_relative_to(package_root / "skills")
                and resolved.suffix == ".md"):
            counts = entry("resources", resolved.relative_to(package_root).as_posix())
            counts["calls"] += 1
            return _ReadCounter(stream, counts)
        return stream

    def source_sha256(*args, **kwargs):
        digest = original_sha256(*args, **kwargs)
        caller = sys._getframe(1)
        if caller.f_code.co_name == "_file_sha256":
            path = Path(caller.f_locals["path"]).resolve()
            if path.suffix == ".py" and path.parent == package_root:
                counts = entry("source_hashes", path.name)
                counts["calls"] += 1
                return _HashCounter(digest, counts)
        return digest

    def observed_json(value):
        result = original_json(value)
        if observing:
            observation["json_calls"] += 1
            observation["json_bytes"] += len(result.encode("utf-8"))
        return result

    def observed_sha256(value=b"", *args, **kwargs):
        if observing:
            observation["sha256_calls"] += 1
            observation["sha256_bytes"] += len(value)
        return original_sha256(value, *args, **kwargs)

    def observe(*args, **kwargs):
        nonlocal observing
        observing = True
        try:
            return original_observe(*args, **kwargs)
        finally:
            observing = False

    logger = context_presentation.LOGGER
    prior_level, prior_propagate = logger.level, logger.propagate
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.NullHandler()
    logger.addHandler(handler)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "open", opened))
            stack.enter_context(patch.object(idea_skill, "hashlib", SimpleNamespace(sha256=source_sha256)))
            stack.enter_context(patch.object(context_presentation, "hashlib", SimpleNamespace(sha256=observed_sha256)))
            stack.enter_context(patch.object(context_presentation, "_json", observed_json))
            stack.enter_context(patch.object(context_presentation, "_observe_context_projection", observe))
            binding = adapter.runtime_binding()
            request = _request(runtime_binding=binding)
            phase = "generate"
            draft = adapter.generate_draft(request)
        encoded = json.dumps(draft.draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"effective_log_level": logger.getEffectiveLevel(),
                "info_enabled": logger.isEnabledFor(logging.INFO),
                "preparation": counters, "observation": observation,
                "result_sha256": original_sha256(encoded.encode("utf-8")).hexdigest(),
                "result_utf8_bytes": len(encoded.encode("utf-8")),
                "provider_calls": len(runner.calls)}
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
        logger.propagate = prior_propagate


def test_disabled_diagnostics_do_no_observation_encoding_and_preserve_result(tmp_path):
    enabled = measure_plan_preparation(tmp_path / "enabled", logging.INFO)
    disabled = measure_plan_preparation(tmp_path / "disabled", logging.WARNING)
    assert enabled["effective_log_level"] == logging.INFO and enabled["info_enabled"]
    assert disabled["effective_log_level"] == logging.WARNING and not disabled["info_enabled"]
    assert enabled["result_sha256"] == disabled["result_sha256"]
    assert enabled["result_utf8_bytes"] == disabled["result_utf8_bytes"]
    assert enabled["provider_calls"] == disabled["provider_calls"] == 1
    assert enabled["observation"]["json_calls"] > 0
    assert enabled["observation"]["sha256_calls"] > 0
    assert disabled["observation"] == {
        "json_calls": 0, "json_bytes": 0, "sha256_calls": 0, "sha256_bytes": 0}
    for phase in ("binding", "generate"):
        # These exact source files are shared by the inherited adapter chain;
        # measuring actual digest construction avoids counting cache lookups.
        for filename in ("idea_skill.py", "root_resident_mcp.py", "provider_supervisor.py"):
            assert disabled["preparation"][phase]["source_hashes"][filename]["calls"] == 1
        assert disabled["preparation"][phase]["resources"]["skills/plan_stage/SKILL.md"]["calls"] == 1


def test_context_renderer_preserves_canonical_science_and_diagnostic_independence():
    pack = {"cycle_ref": "cycle-1", "accepted_formal_plan_binding": {
        "plan_document": {"notes": "限制、失败、未评价。" * 10000,
            "unknown": None, "values": [False, 1, "quoted\"field"]}}}
    arguments = {"context_pack_ref": "context-1", "context_pack_hash": "a" * 64}
    logger = context_presentation.LOGGER
    prior = logger.level
    try:
        logger.setLevel(logging.WARNING)
        expected = canonical_json(context_presentation.stage_context_view("bundle", pack, **arguments))
        disabled = context_presentation.stage_context_json("bundle", pack, **arguments)
        logger.setLevel(logging.INFO)
        enabled = context_presentation.stage_context_json("bundle", pack, **arguments)
    finally:
        logger.setLevel(prior)
    assert enabled == disabled == expected
    assert json.loads(enabled)["sections"]["accepted_formal_plan_binding"] == pack["accepted_formal_plan_binding"]


if __name__ == "__main__":
    output_path = Path(sys.argv[1])
    with tempfile.TemporaryDirectory(prefix="context-preparation-cost-") as temporary:
        measurements = {name: measure_plan_preparation(Path(temporary) / name, level)
            for name, level in (("enabled", logging.INFO), ("disabled", logging.WARNING))}
    measurements["environment"] = {"python": sys.version, "platform": platform.platform(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": {name: hashlib.sha256(Path(idea_skill.__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("idea_skill.py", "plan_skill.py", "context_presentation.py")}}
    output_path.write_text(json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(measurements, ensure_ascii=False, sort_keys=True))

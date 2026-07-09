"""CP8.1 · execution_manifest 契约 + harness manifest 适配层（步⑧）。

验收面：schema 结构校验（kind 条件必填）/ manifest↔plan 切片交叉核（防「新 plan 旁路」）/
staging 物化（原子+哨兵+篡改核验）/ 命令解析围栏（占位符、路径、env、超时）/ 真子进程端到端。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from orchestrator import manifest as MF
from orchestrator.schemas import SchemaSet

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _slice(**over):
    """resolved plan 切片（冻结 plan.schema 的 target 原样 + 编排器派生绑定四件，见 manifest.py 模块注释）。"""
    s = {"target_key": "t1", "target_kind": "build", "seq": 1, "critical": True,
         "budget_estimate": 1.0, "spec_md": "训练线性 toy 基线并出厂评估",
         "claim": {"canonical_key": "ck-toy", "slug": "toy-b"},
         "protocol_id": 1, "protocol_ver": 1, "eval_key": "t1", "target_set_hash": "tsh-1"}
    s.update(over)
    return s


def _manifest(sl, **over):
    m = {"manifest_version": 1,
         "target_ref": {"target_key": sl["target_key"], "target_kind": sl["target_kind"],
                        "seq": sl["seq"], "plan_slice_hash": MF.canon_hash(sl)},
         "protocol_ref": {"protocol_id": sl["protocol_id"], "protocol_ver": sl["protocol_ver"]},
         "env_hash": "toy-env", "config_json": {"lr": 0.1},
         "code_files": ["train.py", "eval.py"],
         "commands": {"smoke": {"argv": ["python", "{src}/train.py", "--smoke"]},
                      "train": {"argv": ["python", "{src}/train.py"], "timeout_s": 60},
                      "eval": {"argv": ["python", "{src}/eval.py", "--ckpt", "{ckpt}"]}},
         "expected_outputs": {"checkpoint": "ckpt.bin"},
         "repro_cmd_md": "python train.py 后 python eval.py --ckpt <ckpt>"}
    m.update(over)
    return m


# ============ schema 结构校验 ============
def test_valid_build_manifest_passes():
    MF.validate_manifest(SCHEMAS, _manifest(_slice()))


def test_policy_yaml_conforms_schema():
    """policy.yaml ↔ policy.schema.json 同步（execution 节随本检查点新增，两侧须一致）。"""
    SCHEMAS.validator("policy").validate(POLICY)
    assert POLICY["execution"]["max_timeout_s"] >= POLICY["execution"]["default_timeout_s"]


def test_build_missing_train_rejected():
    m = _manifest(_slice())
    del m["commands"]["train"]
    with pytest.raises(MF.ManifestError, match="schema"):
        MF.validate_manifest(SCHEMAS, m)


def test_eval_kind_must_not_carry_train():
    sl = _slice(target_kind="eval")
    m = _manifest(sl)
    m["target_ref"]["target_kind"] = "eval"
    with pytest.raises(MF.ManifestError, match="schema"):
        MF.validate_manifest(SCHEMAS, m)   # eval 目标带 train/smoke 命令 = 不成套
    m["commands"] = {"eval": {"argv": ["python", "{src}/eval.py"]}}
    del m["expected_outputs"], m["repro_cmd_md"]
    MF.validate_manifest(SCHEMAS, m)


def test_code_files_reserved_and_abs_rejected():
    m = _manifest(_slice(), code_files=["identity.md"])
    with pytest.raises(MF.ManifestError):
        MF.validate_manifest(SCHEMAS, m)
    m2 = _manifest(_slice(), code_files=["/abs/train.py"])
    with pytest.raises(MF.ManifestError):
        MF.validate_manifest(SCHEMAS, m2)


def test_shellish_string_command_rejected():
    m = _manifest(_slice())
    m["commands"]["train"] = {"argv": "python train.py && rm -rf /"}   # 字符串非数组 = 禁 shell 的 schema 面
    with pytest.raises(MF.ManifestError, match="schema"):
        MF.validate_manifest(SCHEMAS, m)


# ============ 交叉核（防旁路）============
def test_cross_check_happy():
    sl = _slice()
    MF.cross_check(_manifest(sl), sl)


def test_cross_check_rejects_target_drift():
    sl = _slice()
    m = _manifest(sl)
    m["target_ref"]["target_key"] = "t9"
    with pytest.raises(MF.ManifestError, match="target_key"):
        MF.cross_check(m, sl)


def test_cross_check_rejects_slice_hash_mismatch():
    sl = _slice()
    m = _manifest(sl)
    with pytest.raises(MF.ManifestError, match="plan_slice_hash"):
        MF.cross_check(m, _slice(spec_md="换了内容的切片"))   # manifest 回引的不是这份切片


def test_cross_check_rejects_protocol_swap():
    sl = _slice()
    m = _manifest(sl)
    m["protocol_ref"]["protocol_ver"] = 2
    with pytest.raises(MF.ManifestError, match="protocol"):
        MF.cross_check(m, sl)


def test_cross_check_config_obeys_plan_claim():
    sl = _slice(claim={"canonical_key": "ck-toy", "slug": "toy-b", "config_json": {"lr": 0.5}})
    m = _manifest(sl)
    m["target_ref"]["plan_slice_hash"] = MF.canon_hash(sl)
    with pytest.raises(MF.ManifestError, match="config_json"):
        MF.cross_check(m, sl)               # 计划声明 lr=0.5，manifest 写 lr=0.1 → 拒
    m["config_json"] = {"lr": 0.5}
    MF.cross_check(m, sl)                   # 照办即过
    MF.cross_check(_manifest(_slice()), _slice())   # 计划未声明配置 → bundle 自由细化


# ============ staging 物化 ============
def _envelope():
    return {"identity.md": "# toy 基线\n结构: 线性\n\n## 复现命令\npython train.py",
            "train.py": "print('train')",
            "eval.py": "print('eval')",
            "cfg.json": {"lr": 0.1}}


def test_stage_and_resume_roundtrip(tmp_path):
    sl = _slice()
    m = _manifest(sl, code_files=["train.py", "eval.py", "cfg.json", "pkg/util.py"])
    files = {**_envelope(), "pkg/util.py": "X = 1"}
    dest = tmp_path / "t1"
    assert MF.staged_hashes(dest) is None            # 未物化
    ledger = MF.stage_bundle_files(files, m, dest)
    assert set(ledger) == {"train.py", "eval.py", "cfg.json", "pkg/util.py",
                           MF.IDENTITY_FILE, MF.MANIFEST_FILE}
    assert (dest / "pkg" / "util.py").read_text(encoding="utf-8") == "X = 1"
    assert json.loads((dest / "cfg.json").read_text(encoding="utf-8")) == {"lr": 0.1}
    assert MF.staged_hashes(dest) == ledger          # 恢复路径：哨兵在 + 哈希全对 → 返回记账


def test_staged_tamper_detected(tmp_path):
    m = _manifest(_slice(), code_files=["train.py", "eval.py"])
    dest = tmp_path / "t1"
    MF.stage_bundle_files(_envelope(), m, dest)
    (dest / "train.py").write_text("print('EVIL')", encoding="utf-8")
    with pytest.raises(MF.ManifestError, match="损毁"):
        MF.staged_hashes(dest)                       # 被改写不得续跑


def test_staged_extra_file_detected(tmp_path):
    m = _manifest(_slice(), code_files=["train.py", "eval.py"])
    dest = tmp_path / "t1"
    MF.stage_bundle_files(_envelope(), m, dest)
    (dest / "orphan.py").write_text("EVIL = 1", encoding="utf-8")
    with pytest.raises(MF.ManifestError, match="记账外"):
        MF.staged_hashes(dest)                       # 反向对账：记账外文件同样= 损毁（可被合法代码 import 到）


def test_restage_wipes_previous_orphans(tmp_path):
    dest = tmp_path / "t1"
    m1 = _manifest(_slice(), code_files=["train.py", "eval.py", "extra.py"])
    MF.stage_bundle_files({**_envelope(), "extra.py": "X = 1"}, m1, dest)
    m2 = _manifest(_slice(), code_files=["train.py", "eval.py"])   # 换产物重物化（如 Codex 重调）
    ledger = MF.stage_bundle_files(_envelope(), m2, dest)
    assert not (dest / "extra.py").exists()          # 净土物化：上一次的孤儿不残留
    assert MF.staged_hashes(dest) == ledger


def test_stage_rejects_missing_or_bad_paths(tmp_path):
    m = _manifest(_slice(), code_files=["ghost.py"])
    with pytest.raises(MF.ManifestError, match="不在信封"):
        MF.stage_bundle_files(_envelope(), m, tmp_path / "a")
    m2 = _manifest(_slice(), code_files=["../escape.py"])
    with pytest.raises(MF.ManifestError, match="逃逸|相对"):
        MF.stage_bundle_files({**_envelope(), "../escape.py": "x"}, m2, tmp_path / "b")
    files = dict(_envelope())
    del files["identity.md"]
    with pytest.raises(MF.ManifestError, match="identity"):
        MF.stage_bundle_files(files, _manifest(_slice()), tmp_path / "c")
    assert MF.staged_hashes(tmp_path / "a") is None  # 拒绝路径不留哨兵（重物化幂等）


# ============ 命令解析 / 围栏 ============
def _pol(allow=()):
    return {"execution": {"default_timeout_s": 5, "max_timeout_s": 10, "path_allowlist": list(allow)}}


def test_resolve_substitutes_src_and_ckpt(tmp_path):
    m = _manifest(_slice())
    src, ck = tmp_path / "src", tmp_path / "run1" / "ckpt.bin"
    rc = MF.resolve_command(m, "eval", src_dir=src, work_root=tmp_path, policy=_pol(), ckpt_path=ck)
    assert rc.argv == ["python", f"{src.resolve()}/eval.py", "--ckpt", str(ck.resolve())]
    assert rc.timeout_s == 5                          # 未声明 → default
    rc_t = MF.resolve_command(m, "train", src_dir=src, work_root=tmp_path, policy=_pol())
    assert rc_t.timeout_s == 10                       # 声明 60 → max 截断


def test_ckpt_placeholder_only_in_eval(tmp_path):
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "{ckpt}"]
    with pytest.raises(MF.ManifestError, match="ckpt"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    with pytest.raises(MF.ManifestError, match="checkpoint"):
        MF.resolve_command(_manifest(_slice()), "eval", src_dir=tmp_path, work_root=tmp_path, policy=_pol())


def test_path_fence(tmp_path):
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "/etc/passwd"]
    with pytest.raises(MF.ManifestError, match="绝对路径"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    m["commands"]["train"]["argv"] = ["python", "--in=/etc/passwd"]   # --flag=/path 形态也覆盖
    with pytest.raises(MF.ManifestError, match="绝对路径"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    m["commands"]["train"]["argv"] = ["python", "../escape.py"]
    with pytest.raises(MF.ManifestError, match="\\.\\."):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    # 允许：work_root 内绝对路径 / allowlist 前缀 / 非路径样 token（--range=1..5）/ argv[0] 程序名豁免
    m["commands"]["train"]["argv"] = [sys.executable, f"--out={tmp_path}/x", "--range=1..5", "/data/eeg/s1"]
    rc = MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(allow=["/data/eeg"]))
    assert rc.argv[0] == sys.executable


@pytest.mark.parametrize("prog", ["bash", "sh", "/bin/bash", "zsh", "dash", "env"])
def test_shell_launcher_rejected(tmp_path, prog):
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = [prog, "-c", "python train.py"]
    with pytest.raises(MF.ManifestError, match="shell"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())


def test_env_command_form_rejected(tmp_path):
    """`env sh -c …` / `/usr/bin/env bash …` 也被拒（argv[0]=env）——设环境变量走 manifest.env 字段。"""
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["/usr/bin/env", "bash", "-c", "rm -rf /"]
    with pytest.raises(MF.ManifestError, match="shell"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())


def test_checkpoint_dest_fences_traversal(tmp_path):
    run_dir = tmp_path / "run1"
    assert MF.checkpoint_dest(_manifest(_slice()), run_dir) == run_dir / "ckpt.bin"
    m = _manifest(_slice())
    m["expected_outputs"]["checkpoint"] = "../../etc/evil"   # schema pattern + checkpoint_dest 双拒
    with pytest.raises(MF.ManifestError):
        MF.validate_manifest(SCHEMAS, m)
    with pytest.raises(MF.ManifestError, match="\\.\\.|逃逸"):
        MF.checkpoint_dest(m, run_dir)


def test_staged_symlink_dir_detected(tmp_path):
    m = _manifest(_slice(), code_files=["train.py", "eval.py"])
    dest = tmp_path / "t1"
    MF.stage_bundle_files(_envelope(), m, dest)
    (tmp_path / "outside").mkdir()
    (dest / "evilpkg").symlink_to(tmp_path / "outside")   # symlink-dir 可被 import 却不算 names extra
    with pytest.raises(MF.ManifestError, match="symlink"):
        MF.staged_hashes(dest)


def test_staged_sentinel_corruption_is_manifest_error(tmp_path):
    m = _manifest(_slice(), code_files=["train.py", "eval.py"])
    dest = tmp_path / "t1"
    MF.stage_bundle_files(_envelope(), m, dest)
    (dest / MF._SENTINEL).write_text("{not json", encoding="utf-8")
    with pytest.raises(MF.ManifestError, match="哨兵"):
        MF.staged_hashes(dest)                            # 裸 JSONDecodeError 不外泄


def test_staged_ledger_content_validated(tmp_path):
    """哨兵 ledger 内容也校验（codex 第2轮 SHOULD）：坏 key/坏 hash 值统一 ManifestError，不泄原生异常。"""
    m = _manifest(_slice(), code_files=["train.py", "eval.py"])
    dest = tmp_path / "t1"
    MF.stage_bundle_files(_envelope(), m, dest)
    (dest / MF._SENTINEL).write_text(json.dumps({"files": {"../x": "a" * 64}}), encoding="utf-8")
    with pytest.raises(MF.ManifestError):                 # key 是 ../x → 路径围栏拒，不拿去拼路径
        MF.staged_hashes(dest)
    (dest / MF._SENTINEL).write_text(json.dumps({"files": {"train.py": 12345}}), encoding="utf-8")
    with pytest.raises(MF.ManifestError, match="哈希非法"):  # hash 是整数 → 不泄 want[:12] 的 TypeError
        MF.staged_hashes(dest)


@pytest.mark.parametrize("bad", ["./train.py", "pkg/./util.py", ".", "./identity.md"])
def test_dot_segment_rejected(tmp_path, bad):
    """`.` 段（含裸 `.` 与 `./identity.md` 保留名别名）schema + runtime 双拒（codex 第2轮 SHOULD）。"""
    m = _manifest(_slice(), code_files=[bad])
    with pytest.raises(MF.ManifestError):
        MF.validate_manifest(SCHEMAS, m)
    with pytest.raises(MF.ManifestError, match="逃逸|相对|保留名"):
        MF.stage_bundle_files({**_envelope(), bad: "x"}, m, tmp_path / bad.replace("/", "_"))


def test_dotfile_still_allowed(tmp_path):
    """真 dotfile（.gitignore/.hidden）不受 `.` 段规则误伤——段名是 `.hidden` 非纯 `.`。"""
    m = _manifest(_slice(), code_files=["train.py", "eval.py", ".gitignore"])
    MF.validate_manifest(SCHEMAS, m)
    MF.stage_bundle_files({**_envelope(), ".gitignore": "*.pyc"}, m, tmp_path / "t1")


def test_env_overlay_preserves_inherited(tmp_path):
    """harness env = os.environ overlay（非 replacement）：子进程既见 manifest env 又见继承的 PATH。"""
    os.environ["CP81_PROBE"] = "inherited"
    try:
        m = _manifest(_slice())
        m["commands"]["train"]["argv"] = [
            sys.executable, "-c",
            "import os; assert os.environ['TOY_SEED']=='7'; assert os.environ['CP81_PROBE']=='inherited'; "
            "assert os.environ.get('PATH'); print('env ok')"]
        m["env"] = {"TOY_SEED": "7"}
        MF.stage_bundle_files({"identity.md": "# x", "train.py": "x=1", "eval.py": "y=1"}, m, tmp_path / "src")
        r = MF.run_manifest_command(m, "train", staging_dir=str(tmp_path / "run1"), log_name="t.log",
                                    src_dir=tmp_path / "src", work_root=tmp_path, policy=POLICY)
        assert r["exit_code"] == 0, Path(r["log_path"]).read_text(encoding="utf-8")
    finally:
        del os.environ["CP81_PROBE"]


def test_env_fence(tmp_path):
    m = _manifest(_slice(), env={"PATH": "/evil"})
    with pytest.raises(MF.ManifestError, match="PATH"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    m2 = _manifest(_slice())
    m2["env"] = {"bad-name": "x"}                     # schema propertyNames 也拒；围栏面独立再执法
    with pytest.raises(MF.ManifestError, match="键名"):
        MF.resolve_command(m2, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    m3 = _manifest(_slice(), env={"TOY_SEED": "7"})
    assert MF.resolve_command(m3, "train", src_dir=tmp_path, work_root=tmp_path,
                              policy=_pol()).env == {"TOY_SEED": "7"}


def test_missing_command_kind_rejected(tmp_path):
    sl = _slice(target_kind="eval")
    m = _manifest(sl)
    m["target_ref"]["target_kind"] = "eval"
    m["commands"] = {"eval": {"argv": ["python", "e.py"]}}
    with pytest.raises(MF.ManifestError, match="commands.train"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())


# ============ 真子进程端到端（manifest → harness.run_staged）============
def test_run_manifest_command_end_to_end(tmp_path):
    sl = _slice()
    m = _manifest(sl)
    m["commands"]["train"]["argv"] = [sys.executable, "{src}/train.py"]
    m["commands"]["eval"]["argv"] = [sys.executable, "{src}/eval.py", "{ckpt}"]
    files = {"identity.md": "# toy\n复现: 见 manifest",
             "train.py": "import pathlib; pathlib.Path('ckpt.bin').write_text('w1'); print('loss: 0.2')",
             "eval.py": "import sys, pathlib; assert pathlib.Path(sys.argv[1]).read_text() == 'w1'; "
                        "print('metric_value: 1@1=0.93')"}
    src = tmp_path / "t1" / "src"
    MF.stage_bundle_files(files, m, src)
    run_dir = tmp_path / "t1" / "run1"
    r = MF.run_manifest_command(m, "train", staging_dir=str(run_dir), log_name="train.log",
                                src_dir=src, work_root=tmp_path, policy=POLICY)
    assert r["exit_code"] == 0 and (run_dir / "ckpt.bin").read_text() == "w1"
    ev = MF.run_manifest_command(m, "eval", staging_dir=str(tmp_path / "t1" / "eval1"), log_name="eval.log",
                                 src_dir=src, work_root=tmp_path, policy=POLICY,
                                 ckpt_path=run_dir / "ckpt.bin")
    assert ev["exit_code"] == 0
    assert "metric_value: 1@1=0.93" in Path(ev["log_path"]).read_text(encoding="utf-8")

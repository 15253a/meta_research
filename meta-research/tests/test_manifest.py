"""CP8.1 · execution_manifest 契约 + harness manifest 适配层（步⑧）。

验收面：schema 结构校验（kind 条件必填）/ manifest↔plan 切片交叉核（防「新 plan 旁路」）/
staging 物化（原子+哨兵+篡改核验）/ 命令解析围栏（占位符、路径、env、超时）/ 真子进程端到端。
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
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


def _manifest_with_asset_refs(*refs):
    manifest = _manifest(_slice())
    manifest["commands"]["smoke"]["argv"].extend(
        "{asset:" + ref + "}" for ref in reversed(refs))
    manifest["commands"]["train"]["argv"].extend(
        "{asset:" + ref + "}" for ref in refs)
    if refs:  # 同一 ref 可在多个 command 中使用，但快照只冻结唯一授权集合
        manifest["commands"]["eval"]["argv"].append("{asset:" + refs[0] + "}")
    return manifest


def _fake_asset_identities(work_root: Path, refs):
    """授权快照结构单测不依赖 DB/实体文件；只构造严格 canonical 的生成时身份。"""
    identities = {}
    for ref in refs:
        request_id, item_no, asset_no = MF._parse_canonical_asset_ref(ref)
        managed_path = work_root / "input" / "user_provided" / str(request_id) / str(item_no) / f"asset-{asset_no}"
        identities[ref] = MF.AssetIdentity(
            ref=ref, request_id=request_id, item_no=item_no, asset_no=asset_no,
            sha256="b" * 64, size_bytes=9, managed_path=str(managed_path.resolve()))
    return identities


def _rewrite_staged_ledger_file(dest: Path, rel: str, payload) -> None:
    """测试专用：同步改文件与哨兵哈希，让 loader 的结构校验而非外层篡改检测接到坏 payload。"""
    import hashlib
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (dest / rel).write_bytes(data)
    sentinel = json.loads((dest / MF._SENTINEL).read_text(encoding="utf-8"))
    sentinel["files"][rel] = hashlib.sha256(data).hexdigest()
    (dest / MF._SENTINEL).write_text(json.dumps(sentinel, sort_keys=True), encoding="utf-8")


def test_asset_authorization_snapshot_roundtrip_and_actual_ref_subset(tmp_path):
    refs = ["user-file-request:r7:item:1:asset:2", "user-file-request:r7:item:1:asset:1"]
    manifest = _manifest_with_asset_refs(*refs)
    assert MF.extract_manifest_asset_refs(manifest) == sorted(refs)
    dest = tmp_path / "authorized"
    identities = _fake_asset_identities(tmp_path, refs)
    ledger = MF.stage_bundle_files(
        _envelope(), manifest, dest, authorization_pack_hash="a" * 64,
        allowed_asset_refs=[*refs, "user-file-request:r8:item:1:asset:1"],
        asset_identities=identities)
    assert MF.ASSET_AUTHORIZATION_FILE in ledger
    auth = MF.load_asset_authorization(dest, manifest)
    assert auth.pack_hash == "a" * 64
    assert auth.asset_refs == frozenset(refs)  # 不冻结 ContextPack 中未被 manifest 实际引用的额外权限
    assert auth.identities == identities
    payload = json.loads((dest / MF.ASSET_AUTHORIZATION_FILE).read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert [item["ref"] for item in payload["assets"]] == sorted(refs)


def test_stage_bundle_fsync_topology_makes_sentinel_last(tmp_path, monkeypatch):
    """payload 文件→深层目录→新建祖先须先持久化；sentinel 自身 fsync+rename 后最后再刷 src。"""
    ref = "user-file-request:r7:item:1:asset:1"
    manifest = _manifest_with_asset_refs(ref)
    manifest["code_files"].append("pkg/util.py")
    files = {**_envelope(), "pkg/util.py": "VALUE = 1"}
    dest = tmp_path / "work" / "c1" / "t1" / "src"
    events = []
    original_fsync = MF.os.fsync

    def track_fsync(fd):
        info = os.fstat(fd)
        events.append(("dir" if stat.S_ISDIR(info.st_mode) else "file",
                       Path(os.readlink(f"/proc/self/fd/{fd}"))))
        return original_fsync(fd)

    monkeypatch.setattr(MF.os, "fsync", track_fsync)
    ledger = MF.stage_bundle_files(
        files, manifest, dest, authorization_pack_hash="a" * 64,
        allowed_asset_refs=[ref], asset_identities=_fake_asset_identities(tmp_path, [ref]))

    file_events = [path for kind, path in events if kind == "file"]
    for rel in ledger:
        final = dest / rel
        assert final.exists()
        assert final.with_name(final.name + ".partial") in file_events
    sentinel_tmp = dest / (MF._SENTINEL + ".partial")
    sentinel_fsync_index = events.index(("file", sentinel_tmp))
    assert (dest / MF._SENTINEL).is_file() and not sentinel_tmp.exists()

    # payload 目录自底向上；随后持久化 mkdir(parents=True) 新建的 t1/c1/work 及既有锚 tmp_path。
    pre_sentinel_dirs = [path for no, (kind, path) in enumerate(events)
                         if kind == "dir" and no < sentinel_fsync_index]
    expected_chain = [dest / "pkg", dest, dest.parent, dest.parent.parent,
                      dest.parent.parent.parent, tmp_path]
    positions = [pre_sentinel_dirs.index(path) for path in expected_chain]
    assert positions == sorted(positions)
    # sentinel tmp 的文件 fsync 之后必须还有一次 dest_dir fsync，持久化最终 rename。
    assert any(kind == "dir" and path == dest
               for kind, path in events[sentinel_fsync_index + 1:])


def test_stage_bundle_final_fsync_failure_withdraws_sentinel_and_preserves_original_error(
        tmp_path, monkeypatch):
    """sentinel rename 后确认失败不得留下假 committed；撤回 fsync 再失败也不能覆盖第一次错误。"""
    manifest = _manifest(_slice())
    dest = tmp_path / "durability-failure"
    original_fsync_directory = MF._fsync_directory
    dest_calls = {"count": 0}

    def fail_commit_and_cleanup(path):
        path = Path(path)
        if path == dest:
            dest_calls["count"] += 1
            if dest_calls["count"] == 2:   # payload 目录已成功刷过；这是 sentinel replace 后的确认
                raise OSError("sentinel durability confirmation failed")
            if dest_calls["count"] == 3:   # 撤回后的 best-effort fsync 也失败
                raise RuntimeError("cleanup fsync must not mask original")
        return original_fsync_directory(path)

    monkeypatch.setattr(MF, "_fsync_directory", fail_commit_and_cleanup)
    with pytest.raises(OSError, match="sentinel durability confirmation failed"):
        MF.stage_bundle_files(_envelope(), manifest, dest)

    assert dest_calls["count"] == 3
    assert not (dest / MF._SENTINEL).exists()
    assert not (dest / (MF._SENTINEL + ".partial")).exists()
    assert MF.staged_hashes(dest) is None


def test_asset_authorization_stage_rejects_missing_or_ungranted_context(tmp_path):
    ref = "user-file-request:r7:item:1:asset:1"
    manifest = _manifest_with_asset_refs(ref)
    with pytest.raises(MF.ManifestError, match="缺生成时 ContextPack 授权快照"):
        MF.stage_bundle_files(_envelope(), manifest, tmp_path / "missing")
    with pytest.raises(MF.ManifestError, match="未获生成时 ContextPack 授权"):
        MF.stage_bundle_files(
            _envelope(), manifest, tmp_path / "ungranted",
            authorization_pack_hash="a" * 64,
            allowed_asset_refs=["user-file-request:r8:item:1:asset:1"],
            asset_identities=_fake_asset_identities(tmp_path, [ref]))


def test_asset_authorization_filename_is_reserved_from_code_files(tmp_path):
    manifest = _manifest(_slice(), code_files=[MF.ASSET_AUTHORIZATION_FILE])
    with pytest.raises(MF.ManifestError, match="保留名"):
        MF.stage_bundle_files(
            {**_envelope(), MF.ASSET_AUTHORIZATION_FILE: "attacker supplied"},
            manifest, tmp_path / "reserved")


def test_asset_authorization_tamper_is_caught_by_staging_ledger(tmp_path):
    ref = "user-file-request:r7:item:1:asset:1"
    manifest = _manifest_with_asset_refs(ref)
    dest = tmp_path / "tamper"
    MF.stage_bundle_files(_envelope(), manifest, dest, authorization_pack_hash="a" * 64,
                          allowed_asset_refs=[ref],
                          asset_identities=_fake_asset_identities(tmp_path, [ref]))
    (dest / MF.ASSET_AUTHORIZATION_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(MF.ManifestError, match="损毁|哈希不符"):
        MF.load_asset_authorization(dest, manifest)


@pytest.mark.parametrize("mutate,match", [
    (lambda payload, ref: payload.update(version=True), "version"),
    (lambda payload, ref: payload["assets"].append(dict(payload["assets"][0])), "重复"),
    (lambda payload, ref: payload["assets"][0].update(
        ref="user-file-request:r07:item:1:asset:1"), "canonical"),
    (lambda payload, ref: payload["assets"][0].update(request_id=True), "bool|整数"),
    (lambda payload, ref: payload["assets"][0].update(size_bytes=True), "size_bytes"),
    (lambda payload, ref: payload["assets"][0].update(managed_path="relative/asset-1"), "managed_path"),
])
def test_asset_authorization_snapshot_strict_validation(tmp_path, mutate, match):
    ref = "user-file-request:r7:item:1:asset:1"
    manifest = _manifest_with_asset_refs(ref)
    dest = tmp_path / "strict"
    MF.stage_bundle_files(_envelope(), manifest, dest, authorization_pack_hash="a" * 64,
                          allowed_asset_refs=[ref],
                          asset_identities=_fake_asset_identities(tmp_path, [ref]))
    path = dest / MF.ASSET_AUTHORIZATION_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload, ref)
    _rewrite_staged_ledger_file(dest, MF.ASSET_AUTHORIZATION_FILE, payload)
    with pytest.raises(MF.ManifestError, match=match):
        MF.load_asset_authorization(dest, manifest)


def test_legacy_staging_with_asset_ref_but_no_authorization_is_rejected(tmp_path):
    """模拟旧版本已受 ledger 保护的 staging：manifest 后来包含 ref，但历史格式没有授权快照。"""
    dest = tmp_path / "legacy"
    plain = _manifest(_slice())
    MF.stage_bundle_files(_envelope(), plain, dest)
    with_ref = _manifest_with_asset_refs("user-file-request:r7:item:1:asset:1")
    _rewrite_staged_ledger_file(dest, MF.MANIFEST_FILE, with_ref)
    assert MF.staged_hashes(dest) is not None
    with pytest.raises(MF.ManifestError, match="旧 staging.*缺.*授权快照"):
        MF.load_asset_authorization(dest, with_ref)


def test_legacy_staging_without_asset_ref_remains_compatible(tmp_path):
    manifest = _manifest(_slice())
    dest = tmp_path / "legacy-no-assets"
    MF.stage_bundle_files(_envelope(), manifest, dest)
    assert MF.load_asset_authorization(dest, manifest) is None


# ============ 命令解析 / 围栏 ============
def _pol(allow=()):
    return {"execution": {"default_timeout_s": 5, "max_timeout_s": 10, "path_allowlist": list(allow)}}


def _managed_asset(work_root: Path, body: bytes = b"USER-DATA", *, request_id: int = 7,
                   status: str = "resolved"):
    import hashlib
    ref = f"user-file-request:r{request_id}:item:1:asset:1"
    root = work_root / "input" / "user_provided" / str(request_id)
    asset = root / "1" / "asset-1"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    (root / "assets.manifest.json").write_text(json.dumps({
        "version": 1, "request_id": request_id, "assets": [{
            "ref": ref, "relative_path": "1/asset-1", "sha256": digest, "size_bytes": len(body)}]
    }, sort_keys=True), encoding="utf-8")
    resolution = [{"provided": [{
        "path": str(asset), "ref": ref, "original_relpath": "user-name.txt",
        "hash": digest, "hash_alg": "sha256", "size_bytes": len(body),
    }]}]
    conn = sqlite3.connect(work_root / "research.sqlite")
    conn.execute("CREATE TABLE IF NOT EXISTS interaction_request "
                 "(id INTEGER PRIMARY KEY,status TEXT NOT NULL,resolution_json TEXT)")
    conn.execute("INSERT OR REPLACE INTO interaction_request(id,status,resolution_json) VALUES (?,?,?)",
                 (request_id, status, json.dumps(resolution, sort_keys=True)))
    conn.commit()
    conn.close()
    return ref, asset


def _rewrite_managed_asset_authority(work_root: Path, ref: str, asset: Path, body: bytes) -> None:
    """模拟离线恢复/人工修复同步改写三个当前权威，使 live resolver 本身仍会接受同 ref。"""
    import hashlib
    digest = hashlib.sha256(body).hexdigest()
    asset.write_bytes(body)
    manifest_path = asset.parents[1] / "assets.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"][0]["sha256"] = digest
    payload["assets"][0]["size_bytes"] = len(body)
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    conn = sqlite3.connect(work_root / "research.sqlite")
    resolution = json.loads(conn.execute(
        "SELECT resolution_json FROM interaction_request WHERE id=7").fetchone()[0])
    db_asset = resolution[0]["provided"][0]
    assert db_asset["ref"] == ref
    db_asset["hash"] = digest
    db_asset["size_bytes"] = len(body)
    conn.execute("UPDATE interaction_request SET resolution_json=? WHERE id=7",
                 (json.dumps(resolution, sort_keys=True),))
    conn.commit()
    conn.close()


def test_resolve_substitutes_src_and_ckpt(tmp_path):
    m = _manifest(_slice())
    src, ck = tmp_path / "src", tmp_path / "run1" / "ckpt.bin"
    rc = MF.resolve_command(m, "eval", src_dir=src, work_root=tmp_path, policy=_pol(), ckpt_path=ck)
    assert rc.argv == ["python", f"{src.resolve()}/eval.py", "--ckpt", str(ck.resolve())]
    assert rc.pass_fds == ()                         # 无资产命令保持原执行路径，不要求额外资源生命周期
    assert rc.timeout_s == 5                          # 未声明 → default
    rc_t = MF.resolve_command(m, "train", src_dir=src, work_root=tmp_path, policy=_pol())
    assert rc_t.timeout_s == 10                       # 声明 60 → max 截断


def test_asset_placeholder_resolves_hash_checks_and_runs(tmp_path, monkeypatch):
    """只有 ContextPack+DB 双授权的 ref 可用；已验 fd 跨 Popen 保活，路径替换不能换掉子进程输入。"""
    ref, asset = _managed_asset(tmp_path)
    frozen_identities = MF.capture_asset_identities({ref}, work_root=tmp_path)
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = [
        sys.executable, "-c", "import pathlib,sys; assert pathlib.Path(sys.argv[1]).read_bytes()==b'USER-DATA'",
        "{asset:" + ref + "}"]
    with pytest.raises(MF.ManifestError, match="ContextPack 授权"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol())
    with pytest.raises(MF.ManifestError, match="缺生成时冻结身份"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref})
    rc = MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                            allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)
    assert rc.argv[-1] == f"/proc/self/fd/{rc.pass_fds[0]}"
    for fd in rc.pass_fds:
        os.close(fd)

    original_run_staged = MF.H.run_staged
    inherited_fds = []
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"REPLACED!")

    def replace_after_check(cmd, **kwargs):
        inherited_fds.extend(kwargs["pass_fds"])
        os.replace(replacement, asset)  # resolve 已完成；若子进程二次按原路径打开就会读到坏内容
        return original_run_staged(cmd, **kwargs)

    monkeypatch.setattr(MF.H, "run_staged", replace_after_check)
    result = MF.run_manifest_command(m, "train", staging_dir=str(tmp_path / "run-asset"),
                                     log_name="asset.log", src_dir=tmp_path,
                                     work_root=tmp_path, policy=_pol(), allowed_asset_refs={ref},
                                     expected_asset_identities=frozen_identities)
    assert result["exit_code"] == 0
    assert inherited_fds
    for fd in inherited_fds:
        with pytest.raises(OSError):
            os.fstat(fd)                 # run_manifest_command finally 已关闭父进程 fd

    asset.write_bytes(b"TAMPERED")
    with pytest.raises(MF.ManifestError, match="大小|sha256"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)


def test_asset_authorization_rejects_same_ref_rewritten_to_different_bytes(tmp_path):
    """即使 DB resolution + FS manifest + 实际文件被一起改写成自洽新值，resume/启动也不扩权。"""
    ref, asset = _managed_asset(tmp_path)
    manifest = _manifest_with_asset_refs(ref)
    frozen = MF.capture_asset_identities({ref}, work_root=tmp_path)
    dest = tmp_path / "frozen-authorization"
    MF.stage_bundle_files(
        _envelope(), manifest, dest, authorization_pack_hash="a" * 64,
        allowed_asset_refs={ref}, asset_identities=frozen)
    authorization = MF.load_asset_authorization(dest, manifest)
    assert authorization is not None

    _rewrite_managed_asset_authority(tmp_path, ref, asset, b"DIFFERENT-CONTENT")
    current = MF.resolve_input_asset_ref(ref, work_root=tmp_path)
    try:
        assert current.identity != frozen[ref]  # 三方当前权威自洽，现有 live resolver 本可接受
    finally:
        os.close(current.fd)

    with pytest.raises(MF.ManifestError, match="生成时冻结身份"):
        MF.verify_asset_authorization(authorization, work_root=tmp_path)
    with pytest.raises(MF.ManifestError, match="生成时冻结身份"):
        MF.resolve_command(
            manifest, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
            allowed_asset_refs=authorization.asset_refs,
            expected_asset_identities=authorization.identities)


def test_asset_placeholder_rejects_malformed_duplicate_and_symlink(tmp_path):
    ref, asset = _managed_asset(tmp_path)
    frozen_identities = MF.capture_asset_identities({ref}, work_root=tmp_path)
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "{asset:bad-ref}"]
    with pytest.raises(MF.ManifestError, match="占位符"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref})

    root = tmp_path / "input" / "user_provided" / "7"
    payload = json.loads((root / "assets.manifest.json").read_text())
    payload["assets"].append(dict(payload["assets"][0]))
    (root / "assets.manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    m["commands"]["train"]["argv"] = ["python", "{asset:" + ref + "}"]
    with pytest.raises(MF.ManifestError, match="恰出现一次"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)

    payload["assets"].pop()
    (root / "assets.manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    outside = tmp_path / "outside.bin"; outside.write_bytes(b"USER-DATA")
    asset.unlink(); asset.symlink_to(outside)
    with pytest.raises(MF.ManifestError, match="symlink"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)


@pytest.mark.parametrize("identity_field", ["version", "request_id"])
def test_asset_manifest_identity_rejects_bool(tmp_path, identity_field):
    """True 不得利用 bool 是 int 子类冒充 manifest version/request_id 的整数 1。"""
    ref, _asset = _managed_asset(tmp_path, request_id=1)
    frozen_identities = MF.capture_asset_identities({ref}, work_root=tmp_path)
    manifest_path = tmp_path / "input" / "user_provided" / "1" / "assets.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[identity_field] = True
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "{asset:" + ref + "}"]
    with pytest.raises(MF.ManifestError, match="manifest 身份非法"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)


def test_asset_manifest_symlink_rejected_by_single_fd_open(tmp_path):
    ref, _asset = _managed_asset(tmp_path)
    frozen_identities = MF.capture_asset_identities({ref}, work_root=tmp_path)
    manifest_path = tmp_path / "input" / "user_provided" / "7" / "assets.manifest.json"
    outside = tmp_path / "outside-manifest.json"
    outside.write_bytes(manifest_path.read_bytes())
    manifest_path.unlink()
    manifest_path.symlink_to(outside)
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "{asset:" + ref + "}"]
    with pytest.raises(MF.ManifestError, match="manifest"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)


def test_asset_requires_resolved_database_authority(tmp_path):
    ref, _asset = _managed_asset(tmp_path, status="pending")
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "{asset:" + ref + "}"]
    with pytest.raises(MF.ManifestError, match="非 resolved"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref},
                           expected_asset_identities=_fake_asset_identities(tmp_path, [ref]))


def test_resolve_command_failure_closes_assets_already_opened(tmp_path, monkeypatch):
    ref, _asset = _managed_asset(tmp_path)
    frozen_identities = MF.capture_asset_identities({ref}, work_root=tmp_path)
    m = _manifest(_slice(), env={"PATH": "/forbidden"})  # 资产解析完成后才进入 env 围栏
    m["commands"]["train"]["argv"] = ["python", "{asset:" + ref + "}"]
    original_resolve = MF.resolve_input_asset_ref
    opened = []

    def capture_fd(*args, **kwargs):
        resolved = original_resolve(*args, **kwargs)
        opened.append(resolved.fd)
        return resolved

    monkeypatch.setattr(MF, "resolve_input_asset_ref", capture_fd)
    with pytest.raises(MF.ManifestError, match="PATH"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("field,bad", [
    ("ref", "user-file-request:r7:item:1:asset:2"),
    ("hash", "0" * 64),
    ("size_bytes", 999),
    ("path", "/tmp/not-the-managed-asset"),
])
def test_asset_database_mapping_must_exactly_match_manifest(tmp_path, field, bad):
    ref, _asset = _managed_asset(tmp_path)
    frozen_identities = MF.capture_asset_identities({ref}, work_root=tmp_path)
    db_path = tmp_path / "research.sqlite"
    conn = sqlite3.connect(db_path)
    resolution = json.loads(conn.execute(
        "SELECT resolution_json FROM interaction_request WHERE id=7").fetchone()[0])
    resolution[0]["provided"][0][field] = bad
    conn.execute("UPDATE interaction_request SET resolution_json=? WHERE id=7",
                 (json.dumps(resolution, sort_keys=True),))
    conn.commit()
    conn.close()
    m = _manifest(_slice())
    m["commands"]["train"]["argv"] = ["python", "{asset:" + ref + "}"]
    with pytest.raises(MF.ManifestError, match="resolution"):
        MF.resolve_command(m, "train", src_dir=tmp_path, work_root=tmp_path, policy=_pol(),
                           allowed_asset_refs={ref}, expected_asset_identities=frozen_identities)


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

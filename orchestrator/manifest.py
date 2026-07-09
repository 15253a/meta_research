"""execution_manifest —— bundle 编译产物的机器执行契约：校验/交叉核/物化/围栏/执行（步⑧ CP8.1）。

**分层定位**（设计记录见 ROADMAP 步⑧）：plan 保持抽象（冻结 plan.schema，命令永不入 plan）；
bundle 阶段 Codex 产「代码文件 + identity.md + execution_manifest.json」；本模块是该契约的**唯一执法点**：
- `validate_manifest`：schema 校验（结构合法）；
- `cross_check`：manifest ↔ resolved plan 切片交叉核（防「新 plan 旁路」——manifest 不能自立目标/协议）；
- `stage_bundle_files`：把信封文件物化进 staging（原子 + 哨兵 + sha256 记账，崩溃可续、篡改可辨）；
- `resolve_command` / `run_manifest_command`：占位符解析 + 围栏后委托 harness.run_staged 机械执行。

**resolved plan 切片契约**（attack_stages 在 plan 阶段落进 build_target.plan_ref 的 JSON；两侧共同依赖，
字段名以此为准）：= 冻结 plan.schema 的 target 对象 **原样** + 编排器机械派生的绑定四件
`protocol_id`(int)/`protocol_ver`(int)/`eval_key`(str)/`target_set_hash`(str)。cross_check 消费其中
target_key/target_kind/seq/protocol_id/protocol_ver/claim.config_json；`plan_slice_hash` = canon_hash(切片)。

**围栏 = 防呆非防敌**（诚实边界）：argv 数组禁 shell、cwd 限 run staging、绝对路径 token 须落
work_root 或 policy.execution.path_allowlist 前缀、相对 token 禁 `..` 组件、env 白名单键+禁改装载器
变量、超时上限截断。argv[0]（程序名）豁免路径围栏——解释器/工具允许绝对系统路径。对抗性隔离
（Codex 产的代码本身可任意行事）不在本层——真 worktree/沙箱 = 后续硬化步。

**纪律**：本模块零 DB、零事务（纯文件系统 + 纯计算，§6.13 长操作零事务的自然延伸）；
事务与状态机归调用方（attack_stages）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

from . import harness as H

# 保留文件名：manifest 本体与 identity 由本模块按固定名物化；哨兵是物化完成标志——均不得出现在 code_files
MANIFEST_FILE = "execution_manifest.json"
IDENTITY_FILE = "identity.md"
_SENTINEL = "_staged.ok"
_RESERVED = frozenset({MANIFEST_FILE, IDENTITY_FILE, _SENTINEL})

# 禁改环境变量：改它们会换掉解释器/装载器语境，使「同 manifest 同行为」不可回放
_FORBIDDEN_ENV = frozenset({"PATH", "PYTHONPATH", "PYTHONHOME", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"})
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")   # sha256 十六进制（哨兵 ledger 值校验）

# shell 启动器：argv[0] 是它们 = 把「argv 数组」偷换回「shell 字符串解释」（bash -c "…" 重开 shell 通道，
# 绕过禁 shell 契约，codex BLOCKER）。env(1) 同拒——想设环境变量走 manifest 的 env 字段，不是 env 命令
# （env -S/--split-string 亦是 shell 字符串再入口）。basename 比较 → 覆盖绝对/相对/裸名各形态。
_SHELL_LAUNCHERS = frozenset({"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh", "ash", "env"})


class ManifestError(ValueError):
    """契约违规（schema/交叉核/围栏/物化拒）——fail loud，绝不静默兜底成「猜一个命令跑」。"""


class ResolvedCommand(NamedTuple):
    argv: List[str]
    env: Dict[str, str]
    timeout_s: float


def canon_hash(obj: Any) -> str:
    """规范 JSON 哈希（与 attack_stages 同口径：compact + sort_keys + utf-8）——plan_slice_hash 的定义。"""
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 校验 / 交叉核 --
def validate_manifest(schemas, manifest: Any) -> None:
    """schema 结构校验（schemas=SchemaSet）。错误全列出（fail-fast 排障入口，不截断）。"""
    v = schemas.validator("execution_manifest")
    errs = [f"{e.json_path} {e.message}" for e in v.iter_errors(manifest)]
    if errs:
        raise ManifestError("execution_manifest schema 校验失败:\n" + "\n".join(errs))


def cross_check(manifest: Dict[str, Any], plan_slice: Dict[str, Any]) -> None:
    """manifest ↔ resolved plan 切片交叉核（防旁路）：目标三元组一致、切片哈希一致（内容寻址回引，
    覆盖 eval_key/claim/spec 等全部切片字段）、协议绑定一致、配置服从计划（claim.config_json 非空时相等）。"""
    ref = manifest["target_ref"]
    for k in ("target_key", "target_kind", "seq"):
        if ref[k] != plan_slice.get(k):
            raise ManifestError(f"manifest target_ref.{k}={ref[k]!r} ≠ plan 切片 {plan_slice.get(k)!r}")
    want = canon_hash(plan_slice)
    if ref["plan_slice_hash"] != want:
        raise ManifestError(f"plan_slice_hash 不符：manifest {ref['plan_slice_hash'][:12]}… ≠ 切片重算 {want[:12]}…"
                            "——manifest 回引的不是本目标的计划切片")
    pr = manifest["protocol_ref"]
    if (pr["protocol_id"], pr["protocol_ver"]) != (plan_slice.get("protocol_id"), plan_slice.get("protocol_ver")):
        raise ManifestError(f"protocol_ref ({pr['protocol_id']}@{pr['protocol_ver']}) ≠ plan 切片绑定 "
                            f"({plan_slice.get('protocol_id')}@{plan_slice.get('protocol_ver')})——bundle 不得换协议（I1/I2）")
    claim_cfg = (plan_slice.get("claim") or {}).get("config_json")
    if claim_cfg:   # 计划声明了配置 ⇒ bundle 只照办；计划未声明/空 {} ⇒ bundle 自由细化
        if canon_hash(manifest["config_json"]) != canon_hash(claim_cfg):
            raise ManifestError("config_json 与 plan claim.config_json 不一致——计划切片是配置的决定者，bundle 只照办")


# ---------------------------------------------------------------- staging 物化 --
def stage_bundle_files(files: Dict[str, Any], manifest: Dict[str, Any], dest_dir: Path) -> Dict[str, str]:
    """把 bundle 信封文件物化进 dest_dir：code_files 逐个 + identity.md + manifest 本体（规范 JSON）。
    每文件 .partial→原子改名（P6 半成品纪律）；**全部落齐后**写哨兵 _staged.ok（内容 = {relpath: sha256}
    记账，原子）——哨兵在 ⟺ 物化完成。dest_dir 归本模块独占管理：物化前**整目录清空**——崩溃/换产物
    重物化都从净土开始，绝不留上一次的孤儿文件（内审 SHOULD：孤儿可被新代码 import 到）。返回记账 dict。"""
    idmd = files.get(IDENTITY_FILE)
    if not isinstance(idmd, str) or not idmd.strip():
        raise ManifestError("bundle 信封缺 identity.md（或为空）——身份草稿是注册入池的必要输入")
    names = list(manifest["code_files"])
    if len(set(names)) != len(names):
        raise ManifestError(f"code_files 有重复项: {names}")
    plan: List[tuple] = []                     # (relpath, bytes) —— 先全量组装校验，再落盘（半途发现非法不留残料）
    for name in names:
        _check_rel_path(name)
        if name not in files:
            raise ManifestError(f"code_files 声明的 {name!r} 不在信封 files 中——manifest 与产物不成套")
        plan.append((name, _to_bytes(name, files[name])))
    plan.append((IDENTITY_FILE, idmd.encode("utf-8")))
    plan.append((MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")))
    if dest_dir.exists():          # 全量校验通过后才清（半途发现非法不动现场）；目录专属物化产物，清空安全
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ledger: Dict[str, str] = {}
    for rel, data in plan:
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".partial")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        ledger[rel] = hashlib.sha256(data).hexdigest()
    sent_tmp = dest_dir / (_SENTINEL + ".partial")
    sent_tmp.write_text(json.dumps({"files": ledger}, sort_keys=True), encoding="utf-8")
    os.replace(sent_tmp, dest_dir / _SENTINEL)
    return ledger


def staged_hashes(dest_dir: Path) -> Optional[Dict[str, str]]:
    """物化完成探测 + 篡改核验（恢复路径入口）：哨兵缺 → None（未完成，须重物化）；哨兵在 → 逐文件重算
    sha256 对账 + **反向对账**（目录里出现记账外文件同样= 损毁——多余文件可被合法代码 import 到，
    内审 SHOULD）。任何不符 → ManifestError（staging 被改写属数据损毁，须人工核——同 attack_stages
    eval log 补登强校验精神，绝不拿被改写的产物继续跑）。"""
    sent = dest_dir / _SENTINEL
    if not sent.exists():
        return None
    try:                                       # 哨兵损坏须统一报 ManifestError（codex SHOULD：调用方按契约只捕
        raw = json.loads(sent.read_text(encoding="utf-8"))   # 获 ManifestError，裸 JSONDecodeError/KeyError 会变
        ledger = raw["files"]                  # 成非受控崩溃）
        if not isinstance(ledger, dict):
            raise ValueError("files 非对象")
    except (ValueError, KeyError, TypeError) as e:
        raise ManifestError(f"staging 损毁：哨兵 {_SENTINEL} 不可解析（{e}）——须人工核") from e
    for rel, want in ledger.items():
        # ledger 内容也须校验（codex 第2轮 SHOULD）：rel 先过路径围栏（防 `../x`/`./x` 拿去拼路径），
        # want 须 64 位十六进制（防整数等 want[:12] 泄 TypeError）——全部统一成 ManifestError。
        _check_rel_path(rel, what="哨兵 ledger 键", allow_reserved=True)
        if not (isinstance(want, str) and _HASH_RE.match(want)):
            raise ManifestError(f"staging 损毁：哨兵 ledger[{rel!r}] 哈希非法（{want!r}）——须人工核")
        p = dest_dir / rel
        if p.is_symlink() or not p.is_file():  # symlink 指向外部内容可绕哈希/可被 import（codex SHOULD）；须实体文件
            raise ManifestError(f"staging 损毁：{rel} 缺失或非实体文件（symlink/目录）——须人工核")
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != want:
            raise ManifestError(f"staging 损毁：{rel} 哈希不符（记账 {want[:12]}… ≠ 实收 {got[:12]}…）——被改写，拒绝续跑")
    # 反向对账 + symlink 审计（codex SHOULD）：followlinks=False 下 symlink-dir 落在 dirs 不被下潜——须显式
    # 拒（否则 src/evilpkg->/tmp 可被 import 却不算 extra）；记账外实体文件同样拒。
    on_disk = set()
    for root, dirs, names in os.walk(dest_dir, followlinks=False):
        for d in dirs:
            if (Path(root) / d).is_symlink():
                rel = str((Path(root) / d).relative_to(dest_dir))
                raise ManifestError(f"staging 损毁：出现 symlink 目录 {rel}——被外部写入，拒绝续跑")
        for n in names:
            on_disk.add(str((Path(root) / n).relative_to(dest_dir)))
    extra = on_disk - set(ledger) - {_SENTINEL}
    if extra:
        raise ManifestError(f"staging 损毁：出现记账外文件 {sorted(extra)}——被外部写入，拒绝续跑")
    return ledger


def _to_bytes(name: str, payload: Any) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (dict, list)) and name.endswith(".json"):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raise ManifestError(f"信封文件 {name!r} 类型不支持（{type(payload).__name__}）："
                        "代码/文本须为 str，.json 可为对象（canary 不支持二进制产物）")


def _check_rel_path(rel: str, *, what: str = "code_files", allow_reserved: bool = False) -> None:
    """相对 POSIX 路径围栏（code_files / expected_outputs.checkpoint / 哨兵 ledger 键共用）：拒保留名 /
    绝对 / 反斜杠 / 空段 / . / .. 组件——保证解析到 base_dir 内、不逃逸 staging。
    allow_reserved=True 用于 ledger 键校验：ledger 合法含保留名（identity.md / execution_manifest.json），
    只须查路径安全（不拿 ../x 拼路径），不查保留名。"""
    if not allow_reserved and rel in _RESERVED:
        raise ManifestError(f"{what} 不得使用保留名 {rel!r}")
    if not isinstance(rel, str) or rel.startswith("/") or "\\" in rel:
        raise ManifestError(f"{what} 路径须为相对 POSIX 路径: {rel!r}")
    parts = rel.split("/")
    # 拒空段 / `.` / `..`（codex 第2轮 SHOULD）：`.` 段（`./x`、`pkg/./y`、裸 `.`）落盘会被 OS 归一化，
    # 与 ledger 非规范键不符→恢复误判损毁；`./identity.md` 还能绕保留名成别名。`.hidden` 等 dotfile 不受影响。
    if any(p in ("", ".", "..") for p in parts):
        raise ManifestError(f"{what} 路径含空段或 . / ..（非规范/逃逸 staging）: {rel!r}")


def checkpoint_dest(manifest: Dict[str, Any], train_run_dir: Path) -> Path:
    """expected_outputs.checkpoint 的唯一解析入口（codex BLOCKER：schema `^[^/]` 仍放行 `../x`）——
    先过 _check_rel_path（禁 .. / 绝对 / 保留名）再解析到 train_run_dir 内，供 CP8.2 做在场性核验 +
    {ckpt} 传参。保证 checkpoint 只能是「本次 train run 产出的 staging 内文件」。"""
    rel = manifest.get("expected_outputs", {}).get("checkpoint")
    if not rel:
        raise ManifestError("manifest 无 expected_outputs.checkpoint（build/exec 目标须声明训练产物）")
    _check_rel_path(rel, what="expected_outputs.checkpoint")
    return Path(train_run_dir) / rel


# ---------------------------------------------------------------- 命令解析 / 围栏 --
def resolve_command(manifest: Dict[str, Any], kind: str, *, src_dir: Path, work_root: Path,
                    policy: Dict[str, Any], ckpt_path: Optional[Path] = None) -> ResolvedCommand:
    """取 manifest.commands[kind]，做占位符替换（{src}=代码物化目录；{ckpt}=训练产 checkpoint，仅
    eval 命令可用）+ 围栏（见模块注释），返回可直接交 harness 的 ResolvedCommand。"""
    cmd = manifest.get("commands", {}).get(kind)
    if cmd is None:
        raise ManifestError(f"manifest 无 commands.{kind}（目标 kind 与命令集不成套）")
    src = str(Path(src_dir).resolve())
    ck = str(Path(ckpt_path).resolve()) if ckpt_path is not None else None
    argv: List[str] = []
    for i, token in enumerate(cmd["argv"]):
        if "{ckpt}" in token:
            if kind != "eval":
                raise ManifestError(f"{{ckpt}} 只允许出现在 eval 命令（{kind} argv[{i}]={token!r}）——checkpoint 由 train 产出")
            if ck is None:
                raise ManifestError(f"eval argv 用了 {{ckpt}} 但调用方未提供 checkpoint 路径（argv[{i}]={token!r}）")
            token = token.replace("{ckpt}", ck)
        token = token.replace("{src}", src)
        argv.append(token)
    _check_no_shell(argv)                           # argv[0] 禁 shell 启动器（codex BLOCKER：先于路径豁免）
    allow = [str(Path(work_root).resolve())] + [os.path.normpath(p) for p in policy["execution"].get("path_allowlist", [])]
    for i, token in enumerate(argv[1:], start=1):   # argv[0]=程序名豁免（解释器/工具允许绝对系统路径）
        _check_argv_token(token, allow, where=f"argv[{i}]")
    env = dict(manifest.get("env", {}))
    for k in env:
        if not _ENV_NAME_RE.match(k):
            raise ManifestError(f"env 键名非法: {k!r}（须 ^[A-Z][A-Z0-9_]{{0,63}}$）")
        if k in _FORBIDDEN_ENV:
            raise ManifestError(f"env 禁改 {k}（解释器/装载器语境不可由 manifest 改写）")
    ex = policy["execution"]
    timeout = min(float(cmd.get("timeout_s", ex["default_timeout_s"])), float(ex["max_timeout_s"]))
    return ResolvedCommand(argv=argv, env=env, timeout_s=timeout)


def _check_no_shell(argv: List[str]) -> None:
    """argv[0] 禁 shell 启动器（basename 比较，覆盖绝对/相对/裸名）：`bash -c "…"`/`env sh …` 会把
    argv 数组偷换回 shell 字符串解释，绕过禁 shell 契约（codex BLOCKER）。想设环境变量用 manifest.env 字段。
    诚实边界（防呆非防敌）：这挡的是「manifest 命令层重开 shell」这一具体旁路；Codex 产的代码本体一旦
    执行仍可任意行事——对抗性隔离归后续 worktree/沙箱硬化步。"""
    prog = argv[0].rsplit("/", 1)[-1]
    if prog in _SHELL_LAUNCHERS:
        raise ManifestError(f"argv[0]={argv[0]!r} 是 shell 启动器（{prog}）——禁 shell 通道（argv 只跑程序 + 直接参数；"
                            "设环境变量用 manifest.env 字段，不用 env 命令）")


def _check_argv_token(token: str, allow_prefixes: List[str], *, where: str) -> None:
    """路径围栏（按 = 切段以覆盖 --flag=/path 形态）：绝对段须落允许前缀内；相对段禁 .. 组件
    （cwd=run staging，.. 即逃逸）。非路径样段（无 / 且非 ..）不管——不误伤 --range=1..5 之类。"""
    for part in token.split("="):
        if part.startswith("/"):
            p = os.path.normpath(part)
            if not any(p == pre or p.startswith(pre + os.sep) for pre in allow_prefixes):
                raise ManifestError(f"{where} 绝对路径 {part!r} 不在 work_root 或 policy.execution.path_allowlist 内")
        elif ".." in part.split("/"):
            raise ManifestError(f"{where} 相对段 {part!r} 含 .. 组件（cwd 围栏逃逸）")


def run_manifest_command(manifest: Dict[str, Any], kind: str, *, staging_dir: str, log_name: str,
                         src_dir: Path, work_root: Path, policy: Dict[str, Any],
                         ckpt_path: Optional[Path] = None) -> Dict[str, Any]:
    """解析+围栏后委托 harness.run_staged（cwd=staging_dir、.partial→原子改名、.exit 侧车——纪律全继承）。
    返回 run_staged 结果 {exit_code, log_path, log_sha256, log_bytes}；log 入账仍归调用方。"""
    rc = resolve_command(manifest, kind, src_dir=src_dir, work_root=work_root, policy=policy, ckpt_path=ckpt_path)
    return H.run_staged(rc.argv, staging_dir=staging_dir, log_name=log_name,
                        timeout_s=rc.timeout_s, env=rc.env or None)

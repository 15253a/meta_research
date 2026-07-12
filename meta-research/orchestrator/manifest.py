"""execution_manifest —— bundle 编译产物的机器执行契约：校验/交叉核/物化/围栏/执行（步⑧ CP8.1）。

**分层定位**（设计记录见 ROADMAP 步⑧）：plan 保持抽象（含 CPU/GPU 资源意图，命令永不入 plan）；
bundle 阶段 Codex 产「代码文件 + identity.md + execution_manifest.json」；本模块是该契约的**唯一执法点**：
- `validate_manifest`：schema 校验（结构合法）；
- `cross_check`：manifest ↔ resolved plan 切片交叉核（防「新 plan 旁路」——manifest 不能自立目标/协议）；
- `stage_bundle_files`：把信封文件物化进 staging（原子 + 哨兵 + sha256 记账，崩溃可续、篡改可辨）；
- `resolve_command` / `run_manifest_command`：占位符解析 + 围栏后委托 harness.run_staged 机械执行。

**resolved plan 切片契约**（attack_stages 在 plan 阶段落进 build_target.plan_ref 的 JSON；两侧共同依赖，
字段名以此为准）：= 冻结 plan.schema 的 target 对象 **原样** + 编排器机械派生的绑定四件
`protocol_id`(int)/`protocol_ver`(int)/`eval_key`(str)/`target_set_hash`(str)。cross_check 消费其中
target_key/target_kind/seq/protocol_id/protocol_ver/gpu_required/claim.config_json；
`plan_slice_hash` = canon_hash(切片)。

**命令围栏 = 纵深防御**：argv 数组禁 shell、cwd 限 run staging、绝对路径 token 须落
work_root 或 policy.execution.path_allowlist 前缀、相对 token 禁 `..` 组件、env 白名单键+禁改装载器
变量、超时上限截断。argv[0]（程序名）豁免路径围栏——解释器/工具允许绝对系统路径。对抗性隔离由
调用方注入的 pinned DockerExecutionSandbox 承担；本层把核验后的命令、输入 fd 与 CPU/GPU mode 交给它。

**纪律**：本模块不写 DB、不持事务；输入资产启动前只用 ``mode=ro`` 连接复核终态授权，
事务与状态机仍归调用方（attack_stages）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any, Collection, Dict, List, Mapping, NamedTuple, Optional
from urllib.parse import quote

from . import harness as H
from .artifact_capability import (
    ArtifactCapabilityError,
    open_artifact,
    open_directory,
    read_artifact_bytes,
    verify_open_fd,
    verify_tree_fd,
)
from .execution_sandbox import sandbox_workload_environment_hash
from .ids import parse_positive_sqlite_int

# 保留文件名：manifest 本体与 identity 由本模块按固定名物化；哨兵是物化完成标志——均不得出现在 code_files
MANIFEST_FILE = "execution_manifest.json"
IDENTITY_FILE = "identity.md"
_SENTINEL = "_staged.ok"
ASSET_AUTHORIZATION_FILE = "_asset_authorization.json"
_RESERVED = frozenset({MANIFEST_FILE, IDENTITY_FILE, _SENTINEL, ASSET_AUTHORIZATION_FILE})

# 禁改环境变量：改它们会换掉解释器/装载器语境，使「同 manifest 同行为」不可回放
_FORBIDDEN_ENV = frozenset({"PATH", "PYTHONPATH", "PYTHONHOME", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"})
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")   # sha256 十六进制（哨兵 ledger 值校验）
_ASSET_REF_RE = re.compile(
    r"^user-file-request:r([1-9][0-9]*):item:([1-9][0-9]*):asset:([1-9][0-9]*)$")
_ASSET_PLACEHOLDER_RE = re.compile(
    r"\{asset:(user-file-request:r[1-9][0-9]*:item:[1-9][0-9]*:asset:[1-9][0-9]*)\}")
_ASSET_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_ASSET_AUTHORIZATION_MAX_BYTES = 1024 * 1024
_ASSET_AUTHORIZATION_MAX_REFS = 10_000

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
    # 已校验输入资产须一直存活到 Popen；子进程只见 /proc/self/fd/N，避免校验后按路径二次打开的 TOCTOU。
    pass_fds: tuple[int, ...] = ()
    fd_expectations: tuple[tuple[int, str, int, Optional[int], Optional[int]], ...] = ()
    tree_expectations: tuple[tuple[int, Dict[str, str], tuple[str, ...]], ...] = ()


class AssetIdentity(NamedTuple):
    """生成时冻结、启动时复核的单资产内容与位置身份。"""

    ref: str
    request_id: int
    item_no: int
    asset_no: int
    sha256: str
    size_bytes: int
    managed_path: str


class _ResolvedInputAsset(NamedTuple):
    proc_path: str
    fd: int
    identity: AssetIdentity


class AssetAuthorization(NamedTuple):
    """随 bundle staging 冻结的最小授权面：生成 pack 身份 + 实际引用的内容身份。"""

    pack_hash: str
    asset_refs: frozenset[str]
    identities: Dict[str, AssetIdentity]


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
    覆盖 eval_key/claim/spec 等全部切片字段）、协议/GPU access mode 绑定一致、配置服从计划
    （claim.config_json 非空时相等）。"""
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
    planned_gpu = plan_slice.get("gpu_required", False)
    if not isinstance(planned_gpu, bool):
        raise ManifestError("plan 切片 gpu_required 须为 bool")
    if manifest.get("gpu_required", False) is not planned_gpu:
        raise ManifestError(
            "manifest.gpu_required 与 plan 切片不一致——GPU access mode 由 plan 冻结")
    claim_cfg = (plan_slice.get("claim") or {}).get("config_json")
    if claim_cfg:   # 计划声明了配置 ⇒ bundle 只照办；计划未声明/空 {} ⇒ bundle 自由细化
        if canon_hash(manifest["config_json"]) != canon_hash(claim_cfg):
            raise ManifestError("config_json 与 plan claim.config_json 不一致——计划切片是配置的决定者，bundle 只照办")


def _parse_canonical_asset_ref(ref: Any, *, label: str = "输入资产 ref") -> tuple[int, int, int]:
    match = _ASSET_REF_RE.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise ManifestError(f"{label} 非 canonical: {ref!r}")
    try:
        return tuple(parse_positive_sqlite_int(v, label=label) for v in match.groups())
    except ValueError as e:
        raise ManifestError(str(e)) from e


def extract_manifest_asset_refs(manifest: Dict[str, Any]) -> List[str]:
    """机械提取所有 command argv 中的 canonical ``{asset:...}`` 引用，去重后字典序返回。

    这是 bundle 生成时与恢复时共用的唯一引用口径；任何含 ``{asset:`` 却不完全匹配 canonical
    placeholder 的 token 都 fail closed，避免快照漏记一个运行期仍可能解释的别名。
    """
    commands = manifest.get("commands") if isinstance(manifest, dict) else None
    if not isinstance(commands, dict):
        raise ManifestError("manifest.commands 须为对象，无法提取输入资产引用")
    if any(not isinstance(kind, str) for kind in commands):
        raise ManifestError("manifest.commands 键须为字符串")
    refs = set()
    for kind in sorted(commands):
        command = commands[kind]
        argv = command.get("argv") if isinstance(command, dict) else None
        if not isinstance(argv, list):
            raise ManifestError(f"manifest.commands.{kind}.argv 须为数组")
        for index, token in enumerate(argv):
            if not isinstance(token, str):
                raise ManifestError(f"manifest.commands.{kind}.argv[{index}] 须为字符串")
            matches = list(_ASSET_PLACEHOLDER_RE.finditer(token))
            remainder = _ASSET_PLACEHOLDER_RE.sub("", token)
            if "{asset:" in remainder:
                raise ManifestError(
                    f"manifest.commands.{kind}.argv[{index}] 含非法输入资产占位符: {token!r}")
            for match in matches:
                ref = match.group(1)
                _parse_canonical_asset_ref(ref)
                refs.add(ref)
    if len(refs) > _ASSET_AUTHORIZATION_MAX_REFS:
        raise ManifestError(f"manifest 实际输入资产引用超过上限 {_ASSET_AUTHORIZATION_MAX_REFS}")
    return sorted(refs)


def _validated_asset_identity(identity: AssetIdentity, *, label: str) -> AssetIdentity:
    if not isinstance(identity, AssetIdentity):
        raise ManifestError(f"{label} 须为 AssetIdentity")
    request_id, item_no, asset_no = _parse_canonical_asset_ref(identity.ref, label=f"{label} ref")
    if any(type(value) is not int for value in (
            identity.request_id, identity.item_no, identity.asset_no)):
        raise ManifestError(f"{label} request/item/asset 位置须为整数（bool 非法）")
    if ((identity.request_id, identity.item_no, identity.asset_no)
            != (request_id, item_no, asset_no)):
        raise ManifestError(f"{label} ref 与 request/item/asset 位置不一致")
    if not isinstance(identity.sha256, str) or _HASH_RE.fullmatch(identity.sha256) is None:
        raise ManifestError(f"{label} sha256 非法")
    if type(identity.size_bytes) is not int or identity.size_bytes < 0:
        raise ManifestError(f"{label} size_bytes 非法")
    if (not isinstance(identity.managed_path, str) or not os.path.isabs(identity.managed_path)
            or os.path.normpath(identity.managed_path) != identity.managed_path):
        raise ManifestError(f"{label} managed_path 须为规范绝对路径")
    suffix = os.path.join("input", "user_provided", str(request_id), str(item_no), f"asset-{asset_no}")
    if not identity.managed_path.endswith(os.sep + suffix):
        raise ManifestError(f"{label} managed_path 与 canonical ref 位置不一致")
    return identity


def _identity_json(identity: AssetIdentity) -> Dict[str, Any]:
    return {
        "ref": identity.ref,
        "request_id": identity.request_id,
        "item_no": identity.item_no,
        "asset_no": identity.asset_no,
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
        "managed_path": identity.managed_path,
    }


def _authorization_snapshot_bytes(*, pack_hash: str, asset_refs: List[str],
                                  asset_identities: Mapping[str, AssetIdentity]) -> bytes:
    if not isinstance(pack_hash, str) or _HASH_RE.fullmatch(pack_hash) is None:
        raise ManifestError("资产授权快照 pack_hash 须为 64 位小写 sha256")
    if set(asset_identities) != set(asset_refs):
        raise ManifestError("资产授权身份集合须与 manifest 实际 refs 完全一致")
    identities = []
    for ref in asset_refs:
        identity = _validated_asset_identity(
            asset_identities[ref], label=f"生成时资产身份 {ref}")
        if identity.ref != ref:
            raise ManifestError(f"生成时资产身份映射键/ref 不一致: {ref}")
        identities.append(_identity_json(identity))
    return json.dumps(
        {"version": 2, "pack_hash": pack_hash, "assets": identities},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """持久化目录项；拒绝最终组件 symlink，保证 fsync 的是预期目录。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(f"fsync 目标不是目录: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fsync(path: Path, data: bytes) -> None:
    """独占创建临时文件并在发布目录项前把内容/元数据刷到稳定存储。"""
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


# ---------------------------------------------------------------- staging 物化 --
def stage_bundle_files(files: Dict[str, Any], manifest: Dict[str, Any], dest_dir: Path, *,
                       authorization_pack_hash: Optional[str] = None,
                       allowed_asset_refs: Optional[Collection[str]] = None,
                       asset_identities: Optional[Mapping[str, AssetIdentity]] = None) -> Dict[str, str]:
    """把 bundle 信封文件物化进 dest_dir：code_files 逐个 + identity.md + manifest 本体（规范 JSON）。
    若传 ``authorization_pack_hash`` + ``allowed_asset_refs`` + ``asset_identities``，机械验证
    manifest 实际引用是该 ContextPack 授权集合的子集，并把 **pack hash + 实际引用
    ref 的位置/内容身份** 冻结进受 ledger 保护的 v2 授权快照；三者须同时传。
    manifest 含资产 ref 却未传授权上下文时 fail closed；无资产 ref 的旧调用保持兼容且不写快照。
    每文件 .partial→原子改名（P6 半成品纪律）；**全部落齐后**写哨兵 _staged.ok（内容 = {relpath: sha256}
    记账，原子）——哨兵在 ⟺ 物化完成。dest_dir 归本模块独占管理：物化前**整目录清空**——崩溃/换产物
    重物化都从净土开始，绝不留上一次的孤儿文件（内审 SHOULD：孤儿可被新代码 import 到）。返回记账 dict。"""
    idmd = files.get(IDENTITY_FILE)
    if not isinstance(idmd, str) or not idmd.strip():
        raise ManifestError("bundle 信封缺 identity.md（或为空）——身份草稿是注册入池的必要输入")
    actual_asset_refs = extract_manifest_asset_refs(manifest)
    supplied = (authorization_pack_hash is not None, allowed_asset_refs is not None,
                asset_identities is not None)
    if any(supplied) and not all(supplied):
        raise ManifestError(
            "资产授权快照须同时提供 authorization_pack_hash、allowed_asset_refs 与 asset_identities")
    authorization_bytes: Optional[bytes] = None
    if authorization_pack_hash is None:
        if actual_asset_refs:
            raise ManifestError("manifest 使用输入资产 ref，但缺生成时 ContextPack 授权快照")
    else:
        if isinstance(allowed_asset_refs, (str, bytes)):
            raise ManifestError("allowed_asset_refs 须为 canonical ref 集合，不得是字符串")
        allowed_list = list(allowed_asset_refs or ())
        if len(allowed_list) > _ASSET_AUTHORIZATION_MAX_REFS:
            raise ManifestError(f"ContextPack 输入资产授权超过上限 {_ASSET_AUTHORIZATION_MAX_REFS}")
        for ref in allowed_list:
            _parse_canonical_asset_ref(ref, label="ContextPack 输入资产 ref")
        if len(set(allowed_list)) != len(allowed_list):
            raise ManifestError("ContextPack allowed_asset_refs 含重复 ref")
        missing = sorted(set(actual_asset_refs) - set(allowed_list))
        if missing:
            raise ManifestError(f"manifest 使用未获生成时 ContextPack 授权的输入资产 ref: {missing}")
        authorization_bytes = _authorization_snapshot_bytes(
            pack_hash=authorization_pack_hash, asset_refs=actual_asset_refs,
            asset_identities=asset_identities or {})
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
    if authorization_bytes is not None:
        plan.append((ASSET_AUTHORIZATION_FILE, authorization_bytes))
    dest_dir = Path(dest_dir)
    # 记录 mkdir(parents=True) 将新建的祖先链。payload 目录项落齐后自底向上 fsync 到 first_existing，
    # 保证不仅 src 内文件，连 cN/tN/src 这些新目录本身也已在父目录持久化。
    first_existing = dest_dir.parent
    while not first_existing.exists():
        if first_existing == first_existing.parent:
            raise ManifestError(f"bundle staging 找不到已存在祖先目录: {dest_dir}")
        first_existing = first_existing.parent
    if dest_dir.exists():          # 全量校验通过后才清（半途发现非法不动现场）；目录专属物化产物，清空安全
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ledger: Dict[str, str] = {}
    for rel, data in plan:
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".partial")
        _write_fsync(tmp, data)
        os.replace(tmp, target)
        ledger[rel] = hashlib.sha256(data).hexdigest()

    # 先持久化 payload 的全部 rename 与逐层目录创建。os.walk 的深度逆序保证 child 在 parent 之前；
    # 再沿 dest.parent→first_existing 刷新 mkdir(parents=True) 可能创建的 staging 祖先。
    payload_dirs = [Path(root) for root, _dirs, _names in os.walk(dest_dir, followlinks=False)]
    for directory in sorted(payload_dirs, key=lambda p: len(p.parts), reverse=True):
        _fsync_directory(directory)
    ancestor = dest_dir.parent
    while True:
        _fsync_directory(ancestor)
        if ancestor == first_existing:
            break
        ancestor = ancestor.parent

    sent_tmp = dest_dir / (_SENTINEL + ".partial")
    _write_fsync(sent_tmp, json.dumps({"files": ledger}, sort_keys=True).encode("utf-8"))
    sentinel = dest_dir / _SENTINEL
    os.replace(sent_tmp, sentinel)
    # 最后一步：sentinel 目录项稳定。此调用返回后，sentinel 可见蕴含全部 payload 与目录拓扑已持久。
    try:
        _fsync_directory(dest_dir)
    except BaseException:
        # 最终持久化确认失败时，live FS 上的 sentinel 不能留给下次 staged_hashes 当成 committed。
        # 撤回与撤回-fsync 都是 best effort；无论它们是否再次失败，必须保留并重抛第一次确认错误。
        try:
            sentinel.unlink(missing_ok=True)
        except BaseException:
            pass
        try:
            _fsync_directory(dest_dir)
        except BaseException:
            pass
        raise
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
        raw = json.loads(read_artifact_bytes(
            sent, max_bytes=1024 * 1024,
            label="staging sentinel").decode("utf-8"))
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
        try:
            got = hashlib.sha256(read_artifact_bytes(
                p, expected_hash=want,
                label=f"staging payload {rel}")).hexdigest()
        except ArtifactCapabilityError as error:
            raise ManifestError(
                f"staging 损毁：{rel} 缺失/非实体或身份不符——须人工核") from error
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


def _read_authorization_payload(path: Path, *, expected_hash: str) -> Dict[str, Any]:
    """从受 ledger 约束的单个 O_NOFOLLOW fd 读取授权快照，并再次对账该 fd 的实际字节。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as e:
        raise ManifestError("staging 损毁：资产授权快照不可打开") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError("staging 损毁：资产授权快照不是常规文件")
        if info.st_size > _ASSET_AUTHORIZATION_MAX_BYTES:
            raise ManifestError("staging 损毁：资产授权快照超过大小上限")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(256 * 1024, _ASSET_AUTHORIZATION_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _ASSET_AUTHORIZATION_MAX_BYTES:
                raise ManifestError("staging 损毁：资产授权快照超过大小上限")
        data = b"".join(chunks)
    except OSError as e:
        raise ManifestError("staging 损毁：资产授权快照读取失败") from e
    finally:
        os.close(fd)
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ManifestError("staging 损毁：资产授权快照与 ledger 哈希不符")

    def no_duplicate_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ManifestError(f"资产授权快照 JSON 重复键: {key!r}")
            obj[key] = value
        return obj

    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError("资产授权快照 JSON 不可解析") from e
    if not isinstance(payload, dict):
        raise ManifestError("资产授权快照须为对象")
    return payload


def load_asset_authorization(dest_dir: Path, manifest: Dict[str, Any]) -> Optional[AssetAuthorization]:
    """加载并严格验证生成时资产授权快照。

    快照必须受 ``_staged.ok`` ledger 保护，且 refs 与当前受账本保护 manifest 的**实际引用集合完全相等**。
    旧 staging 仅在 manifest 完全不含 asset placeholder 时可无快照兼容；有 ref 无快照直接拒绝。
    """
    manifest_refs = extract_manifest_asset_refs(manifest)
    ledger = staged_hashes(Path(dest_dir))
    if ledger is None:
        raise ManifestError("staging 未完成：缺 _staged.ok，不能加载资产授权快照")
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
    if ledger.get(MANIFEST_FILE) != hashlib.sha256(manifest_bytes).hexdigest():
        raise ManifestError("资产授权加载所用 manifest 与 staging ledger 不是同一产物")
    expected_hash = ledger.get(ASSET_AUTHORIZATION_FILE)
    if expected_hash is None:
        if manifest_refs:
            raise ManifestError("旧 staging 的 manifest 使用输入资产 ref，但缺受账本保护的授权快照")
        return None
    payload = _read_authorization_payload(
        Path(dest_dir) / ASSET_AUTHORIZATION_FILE, expected_hash=expected_hash)
    if set(payload) != {"version", "pack_hash", "assets"}:
        raise ManifestError("资产授权快照字段须严格为 version/pack_hash/assets")
    if type(payload["version"]) is not int or payload["version"] != 2:
        raise ManifestError("资产授权快照 version 须为整数 2（bool/旧版无内容身份均非法）")
    pack_hash = payload["pack_hash"]
    if not isinstance(pack_hash, str) or _HASH_RE.fullmatch(pack_hash) is None:
        raise ManifestError("资产授权快照 pack_hash 须为 64 位小写 sha256")
    raw_assets = payload["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) > _ASSET_AUTHORIZATION_MAX_REFS:
        raise ManifestError("资产授权快照 assets 须为有界数组")
    identities: Dict[str, AssetIdentity] = {}
    for index, raw in enumerate(raw_assets, start=1):
        if not isinstance(raw, dict) or set(raw) != {
                "ref", "request_id", "item_no", "asset_no", "sha256", "size_bytes", "managed_path"}:
            raise ManifestError(f"资产授权快照 assets[{index}] 字段非法")
        identity = AssetIdentity(
            ref=raw["ref"], request_id=raw["request_id"], item_no=raw["item_no"],
            asset_no=raw["asset_no"], sha256=raw["sha256"], size_bytes=raw["size_bytes"],
            managed_path=raw["managed_path"])
        _validated_asset_identity(identity, label=f"资产授权快照 assets[{index}]")
        if identity.ref in identities:
            raise ManifestError(f"资产授权快照 assets 含重复 ref: {identity.ref}")
        identities[identity.ref] = identity
    raw_refs = list(identities)
    if raw_refs != sorted(raw_refs):
        raise ManifestError("资产授权快照 assets 须按 ref 字典序规范排列")
    if raw_refs != manifest_refs:
        raise ManifestError("资产授权快照 refs 与 manifest 实际引用集合不一致")
    return AssetAuthorization(
        pack_hash=pack_hash, asset_refs=frozenset(raw_refs), identities=identities)


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


def _read_asset_manifest(path: Path, *, request_id: int) -> Dict[str, Any]:
    """用一个 ``O_NOFOLLOW`` fd 完成类型/大小检查与读取，避免 stat→read_text 二次按路径打开。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as e:
        raise ManifestError(f"输入资产 manifest 不可打开: r{request_id}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError(f"输入资产 manifest 不是常规文件: r{request_id}")
        if info.st_size > _ASSET_MANIFEST_MAX_BYTES:
            raise ManifestError("输入资产 manifest 超过大小上限")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, _ASSET_MANIFEST_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _ASSET_MANIFEST_MAX_BYTES:
                raise ManifestError("输入资产 manifest 超过大小上限")
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ManifestError(f"输入资产 manifest 不可读/非法: r{request_id}") from e
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise ManifestError(f"输入资产 manifest 身份非法: r{request_id}")
    # bool 是 int 子类；显式 type 检查，避免 True 冒充 schema 中的 1。
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ManifestError(f"输入资产 manifest 身份非法: r{request_id}")
    if type(payload.get("request_id")) is not int or payload["request_id"] != request_id:
        raise ManifestError(f"输入资产 manifest 身份非法: r{request_id}")
    return payload


def _authorize_asset_from_db(*, work: Path, request_id: int, item_no: int, asset_no: int,
                             ref: str, digest: str, size: int, asset_path: Path) -> None:
    """以只读主库终态为授权真相，并与文件清单及规范托管路径逐字段精确对账。"""
    db_path = work / "research.sqlite"
    if db_path.is_symlink():
        raise ManifestError("research.sqlite 不得是 symlink")
    try:
        conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise ManifestError("输入资产授权库 research.sqlite 不可只读打开") from e
    try:
        try:
            row = conn.execute(
                "SELECT status,resolution_json FROM interaction_request WHERE id=?", (request_id,)).fetchone()
        except sqlite3.Error as e:
            raise ManifestError("输入资产授权库缺 interaction_request 或不可查询") from e
    finally:
        conn.close()
    if row is None:
        raise ManifestError(f"输入资产 request r{request_id} 不在授权库")
    if row[0] != "resolved":
        raise ManifestError(f"输入资产 request r{request_id} 非 resolved（{row[0]!r}），拒绝消费")
    try:
        resolution = json.loads(row[1])
    except (TypeError, json.JSONDecodeError) as e:
        raise ManifestError(f"输入资产 request r{request_id} resolution_json 损坏") from e
    if not isinstance(resolution, list) or item_no > len(resolution):
        raise ManifestError(f"输入资产 request r{request_id} resolution_json 无 item {item_no}")
    outcome = resolution[item_no - 1]
    provided = outcome.get("provided") if isinstance(outcome, dict) else None
    if not isinstance(provided, list) or asset_no > len(provided):
        raise ManifestError(f"输入资产 request r{request_id} resolution_json 无 asset {item_no}/{asset_no}")
    db_asset = provided[asset_no - 1]
    if not isinstance(db_asset, dict):
        raise ManifestError(f"输入资产 request r{request_id} resolution asset 非对象")

    # ref 在整个 resolution 中必须唯一，且其数组位置须与 canonical ref 的 item/asset 序号一致。
    ref_count = 0
    for db_outcome in resolution:
        db_provided = db_outcome.get("provided") if isinstance(db_outcome, dict) else None
        if isinstance(db_provided, list):
            ref_count += sum(isinstance(a, dict) and a.get("ref") == ref for a in db_provided)
    if ref_count != 1:
        raise ManifestError(f"输入资产 ref 在 resolution_json 中须恰出现一次: {ref}")

    expected_path = str(asset_path)
    if (db_asset.get("ref") != ref or db_asset.get("hash_alg") != "sha256"
            or db_asset.get("hash") != digest or db_asset.get("path") != expected_path
            or type(db_asset.get("size_bytes")) is not int or db_asset["size_bytes"] != size):
        raise ManifestError(f"输入资产 DB resolution 与 manifest/path 绑定不符: {ref}")


def resolve_input_asset_ref(ref: str, *, work_root: Path) -> _ResolvedInputAsset:
    """解析并授权 opaque ref，返回保持打开的只读 fd（调用方负责关闭）。

    文件系统 manifest 只描述已发布树；``research.sqlite`` 中 resolved ``resolution_json`` 才授予消费权。
    两者与实际 fd 的 canonical ref/path/hash/size 必须全部一致。返回 ``/proc/self/fd/N``，由 harness
    通过 ``pass_fds`` 传给子进程，消除校验后关闭再按路径打开的 TOCTOU 窗口。
    """
    match = _ASSET_REF_RE.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise ManifestError(f"输入资产 ref 非法: {ref!r}")
    try:
        request_id, item_no, asset_no = [
            parse_positive_sqlite_int(v, label="input asset ref") for v in match.groups()]
    except ValueError as e:
        raise ManifestError(str(e)) from e

    work = Path(work_root).resolve()
    input_dir = work / "input"
    managed = input_dir / "user_provided"
    request_dir = managed / str(request_id)
    for candidate in (input_dir, managed, request_dir):
        if candidate.is_symlink():
            raise ManifestError(f"输入资产托管路径不得是 symlink: {candidate}")
    try:
        request_real = request_dir.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise ManifestError(f"输入资产 request r{request_id} 不存在/不可解析") from e
    if work not in request_real.parents:
        raise ManifestError(f"输入资产 request r{request_id} 逃出 work_root")

    payload = _read_asset_manifest(request_real / "assets.manifest.json", request_id=request_id)
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) > 10_000:
        raise ManifestError(f"输入资产 manifest assets 非法: r{request_id}")
    hits = [a for a in assets if isinstance(a, dict) and a.get("ref") == ref]
    if len(hits) != 1:
        raise ManifestError(f"输入资产 ref 在 manifest 中须恰出现一次: {ref}")
    entry = hits[0]
    expected_rel = f"{item_no}/asset-{asset_no}"
    if entry.get("relative_path") != expected_rel:
        raise ManifestError(f"输入资产 ref/path 绑定不符: {ref}")
    digest = entry.get("sha256")
    size = entry.get("size_bytes")
    if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
        raise ManifestError(f"输入资产 sha256 非法: {ref}")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ManifestError(f"输入资产 size_bytes 非法: {ref}")

    item_dir = request_real / str(item_no)
    asset_path = item_dir / f"asset-{asset_no}"
    if item_dir.is_symlink() or asset_path.is_symlink():
        raise ManifestError(f"输入资产路径不得是 symlink: {ref}")
    _authorize_asset_from_db(work=work, request_id=request_id, item_no=item_no, asset_no=asset_no,
                             ref=ref, digest=digest, size=size, asset_path=asset_path)
    identity = AssetIdentity(
        ref=ref,
        request_id=request_id,
        item_no=item_no,
        asset_no=asset_no,
        sha256=digest,
        size_bytes=size,
        managed_path=str(asset_path),
    )
    _validated_asset_identity(identity, label=f"当前输入资产 {ref}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(asset_path), flags)
    except OSError as e:
        raise ManifestError(f"输入资产不可打开: {ref}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ManifestError(f"输入资产大小/类型与 manifest 不符: {ref}")
        hasher = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ManifestError(f"输入资产 sha256 与 manifest 不符: {ref}")
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return _ResolvedInputAsset(proc_path=f"/proc/self/fd/{fd}", fd=fd, identity=identity)


def capture_asset_identities(refs: Collection[str], *, work_root: Path) -> Dict[str, AssetIdentity]:
    """在 bundle 生成边界复核 DB + FS manifest + 实际 fd，冻结 canonical ref 的内容身份。

    每个 fd 只在本函数内保持打开；真正启动时会重新打开并与快照精确比对，然后才传给子进程。
    """
    if isinstance(refs, (str, bytes)):
        raise ManifestError("资产身份捕获 refs 须为 canonical ref 集合，不得是字符串")
    raw_refs = list(refs)
    if len(raw_refs) > _ASSET_AUTHORIZATION_MAX_REFS:
        raise ManifestError(f"资产身份捕获 refs 超过上限 {_ASSET_AUTHORIZATION_MAX_REFS}")
    for ref in raw_refs:
        _parse_canonical_asset_ref(ref, label="资产身份捕获 ref")
    if len(set(raw_refs)) != len(raw_refs):
        raise ManifestError("资产身份捕获 refs 含重复 ref")

    identities: Dict[str, AssetIdentity] = {}
    for ref in sorted(raw_refs):
        asset = resolve_input_asset_ref(ref, work_root=work_root)
        try:
            identities[ref] = asset.identity
        finally:
            os.close(asset.fd)
    return identities


def verify_asset_authorization(authorization: Optional[AssetAuthorization], *, work_root: Path) -> None:
    """在 resume/消费前验证同 ref 仍指向 bundle 生成时冻结的同一位置与字节。"""
    if authorization is None:
        return
    if not isinstance(authorization, AssetAuthorization):
        raise ManifestError("authorization 须为 AssetAuthorization")
    current = capture_asset_identities(authorization.asset_refs, work_root=work_root)
    if current != authorization.identities:
        changed = sorted(
            ref for ref in authorization.asset_refs
            if current.get(ref) != authorization.identities.get(ref))
        raise ManifestError(
            f"输入资产与 bundle 生成时冻结身份不一致: {changed}——拒绝同 ref 消费不同字节")


def _expand_asset_placeholders(
        token: str, *, work_root: Path, allowed_asset_refs: Optional[Collection[str]],
        expected_asset_identities: Optional[Mapping[str, AssetIdentity]], opened_fds: List[int],
        fd_expectations: List[tuple[int, str, int, Optional[int], Optional[int]]]) -> str:
    def replace(match: re.Match) -> str:
        ref = match.group(1)
        if allowed_asset_refs is None or ref not in allowed_asset_refs:
            raise ManifestError(f"输入资产 ref 未获本 ContextPack 授权: {ref}")
        if expected_asset_identities is None or ref not in expected_asset_identities:
            raise ManifestError(f"输入资产 ref 缺生成时冻结身份: {ref}")
        expected = _validated_asset_identity(
            expected_asset_identities[ref], label=f"生成时冻结身份 {ref}")
        if expected.ref != ref:
            raise ManifestError(f"生成时冻结身份映射键/ref 不一致: {ref}")
        asset = resolve_input_asset_ref(ref, work_root=work_root)
        if expected != asset.identity:
            os.close(asset.fd)
            raise ManifestError(
                f"输入资产与 bundle 生成时冻结身份不一致: {ref}——拒绝同 ref 消费不同字节")
        opened_fds.append(asset.fd)
        fd_expectations.append((
            asset.fd, asset.identity.sha256, asset.identity.size_bytes,
            None, None))
        return asset.proc_path

    expanded = _ASSET_PLACEHOLDER_RE.sub(replace, token)
    if "{asset:" in expanded:
        raise ManifestError(f"argv 含非法/未闭合输入资产占位符: {token!r}")
    return expanded


# ---------------------------------------------------------------- 命令解析 / 围栏 --
def resolve_command(manifest: Dict[str, Any], kind: str, *, src_dir: Path, work_root: Path,
                    policy: Dict[str, Any], ckpt_path: Optional[Path] = None,
                    ckpt_content_hash: Optional[str] = None,
                    expected_source_hashes: Optional[Mapping[str, str]] = None,
                    allowed_asset_refs: Optional[Collection[str]] = None,
                    expected_asset_identities: Optional[Mapping[str, AssetIdentity]] = None) -> ResolvedCommand:
    """取 manifest.commands[kind]，做占位符替换（{src}=代码物化目录；{ckpt}=训练产 checkpoint，仅
    eval 命令可用；{asset:ref}=当前 ContextPack 明确授权的用户资产）+ 围栏（见模块注释），返回可直接交
    harness 的 ResolvedCommand。生产调用同时传入生成时 ``expected_asset_identities``，同 ref 重写
    会在启动前被拒绝。含资产时调用方必须把 ``pass_fds`` 传给子进程并最终关闭。"""
    cmd = manifest.get("commands", {}).get(kind)
    if cmd is None:
        raise ManifestError(f"manifest 无 commands.{kind}（目标 kind 与命令集不成套）")
    argv: List[str] = []
    opened_fds: List[int] = []
    fd_expectations: List[tuple[int, str, int, Optional[int], Optional[int]]] = []
    tree_expectations: List[tuple[int, Dict[str, str], tuple[str, ...]]] = []
    try:
        if expected_source_hashes is not None:
            source_hashes = dict(expected_source_hashes)
            if not source_hashes:
                raise ManifestError("expected_source_hashes 不得为空")
            try:
                source_fd = open_directory(src_dir, label="bundle source tree")
                verify_tree_fd(
                    source_fd, source_hashes, label="bundle source tree",
                    exact=True, allowed_extra=(_SENTINEL,))
            except ArtifactCapabilityError as error:
                raise ManifestError(str(error)) from error
            opened_fds.append(source_fd)
            tree_expectations.append((source_fd, source_hashes, (_SENTINEL,)))
            src = f"/proc/self/fd/{source_fd}"
        else:
            src = str(Path(src_dir).resolve())
        if (ckpt_path is None) != (ckpt_content_hash is None):
            raise ManifestError(
                "checkpoint capability 须同时提供 path 与 content_hash")
        ck = None
        if ckpt_path is not None:
            try:
                capability = open_artifact(
                    ckpt_path, expected_hash=ckpt_content_hash,
                    label="checkpoint capability")
            except ArtifactCapabilityError as error:
                raise ManifestError(str(error)) from error
            ck = capability.proc_path
            identity = capability.identity
            fd = capability.detach()
            opened_fds.append(fd)
            fd_expectations.append((
                fd, identity.content_hash, identity.size_bytes,
                identity.device, identity.inode))
        for i, token in enumerate(cmd["argv"]):
            if i == 0 and "{asset:" in token:
                raise ManifestError("输入资产占位符不得作为 argv[0] 可执行程序")
            if "{ckpt}" in token:
                if kind != "eval":
                    raise ManifestError(f"{{ckpt}} 只允许出现在 eval 命令（{kind} argv[{i}]={token!r}）——checkpoint 由 train 产出")
                if ck is None:
                    raise ManifestError(f"eval argv 用了 {{ckpt}} 但调用方未提供 checkpoint 路径（argv[{i}]={token!r}）")
                token = token.replace("{ckpt}", ck)
            token = token.replace("{src}", src)
            token = _expand_asset_placeholders(
                token, work_root=Path(work_root), allowed_asset_refs=allowed_asset_refs,
                expected_asset_identities=expected_asset_identities,
                opened_fds=opened_fds, fd_expectations=fd_expectations)
            argv.append(token)
        _check_no_shell(argv)                       # argv[0] 禁 shell 启动器（codex BLOCKER：先于路径豁免）
        allow = [str(Path(work_root).resolve())] + [
            os.path.normpath(p) for p in policy["execution"].get("path_allowlist", [])]
        # ``ck`` is not a manifest-authored arbitrary path: the orchestrator
        # resolved it from a committed checkpoint row and passes it explicitly.
        # Permit that one exact path even when a legal pool target lives outside
        # work_root; callers remain responsible for rechecking its content hash.
        allowed_fd_paths = {f"/proc/self/fd/{fd}" for fd in opened_fds}
        allow.extend(
            f"/proc/self/fd/{fd}" for fd, _hashes, _extra in tree_expectations)
        if ck is not None:
            allowed_fd_paths.add(os.path.normpath(ck))
        for i, token in enumerate(argv[1:], start=1):  # argv[0]=程序名豁免（解释器/工具允许绝对系统路径）
            _check_argv_token(token, allow, where=f"argv[{i}]", allowed_exact=allowed_fd_paths)
        env = dict(manifest.get("env", {}))
        for k in env:
            if not _ENV_NAME_RE.match(k):
                raise ManifestError(f"env 键名非法: {k!r}（须 ^[A-Z][A-Z0-9_]{{0,63}}$）")
            if k in _FORBIDDEN_ENV:
                raise ManifestError(f"env 禁改 {k}（解释器/装载器语境不可由 manifest 改写）")
        ex = policy["execution"]
        timeout = min(float(cmd.get("timeout_s", ex["default_timeout_s"])), float(ex["max_timeout_s"]))
        return ResolvedCommand(
            argv=argv, env=env, timeout_s=timeout,
            pass_fds=tuple(opened_fds),
            fd_expectations=tuple(fd_expectations),
            tree_expectations=tuple(tree_expectations))
    except BaseException:
        for fd in set(opened_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _check_no_shell(argv: List[str]) -> None:
    """argv[0] 禁 shell 启动器（basename 比较，覆盖绝对/相对/裸名）：`bash -c "…"`/`env sh …` 会把
    argv 数组偷换回 shell 字符串解释，绕过禁 shell 契约（codex BLOCKER）。想设环境变量用 manifest.env 字段。
    诚实边界（防呆非防敌）：这挡的是「manifest 命令层重开 shell」这一具体旁路；Codex 产的代码本体一旦
    执行仍可任意行事——对抗性隔离归后续 worktree/沙箱硬化步。"""
    prog = argv[0].rsplit("/", 1)[-1]
    if prog in _SHELL_LAUNCHERS:
        raise ManifestError(f"argv[0]={argv[0]!r} 是 shell 启动器（{prog}）——禁 shell 通道（argv 只跑程序 + 直接参数；"
                            "设环境变量用 manifest.env 字段，不用 env 命令）")


def _check_argv_token(token: str, allow_prefixes: List[str], *, where: str,
                      allowed_exact: Collection[str] = ()) -> None:
    """路径围栏（按 = 切段以覆盖 --flag=/path 形态）：绝对段须落允许前缀内；相对段禁 .. 组件
    （cwd=run staging，.. 即逃逸）。非路径样段（无 / 且非 ..）不管——不误伤 --range=1..5 之类。"""
    for part in token.split("="):
        if part.startswith("/"):
            p = os.path.normpath(part)
            if p not in allowed_exact and not any(p == pre or p.startswith(pre + os.sep) for pre in allow_prefixes):
                raise ManifestError(f"{where} 绝对路径 {part!r} 不在 work_root 或 policy.execution.path_allowlist 内")
        elif ".." in part.split("/"):
            raise ManifestError(f"{where} 相对段 {part!r} 含 .. 组件（cwd 围栏逃逸）")


def run_manifest_command(manifest: Dict[str, Any], kind: str, *, staging_dir: str, log_name: str,
                         src_dir: Path, work_root: Path, policy: Dict[str, Any],
                         ckpt_path: Optional[Path] = None,
                         ckpt_content_hash: Optional[str] = None,
                         expected_source_hashes: Optional[Mapping[str, str]] = None,
                         allowed_asset_refs: Optional[Collection[str]] = None,
                         expected_asset_identities: Optional[Mapping[str, AssetIdentity]] = None,
                         execution_supervisor=None,
                         execution_context: Optional[Dict[str, Any]] = None,
                         execution_sandbox=None) -> Dict[str, Any]:
    """解析+围栏后委托 harness.run_staged（cwd=staging_dir、.partial→原子改名、.exit 侧车——纪律全继承）。
    返回 run_staged 结果 {exit_code, log_path, log_sha256, log_bytes}；log 入账仍归调用方。"""
    gpu_required = manifest.get("gpu_required", False)
    if (gpu_required is not False and gpu_required is not True):
        raise ManifestError("manifest.gpu_required 须为 bool")
    if (execution_sandbox is not None
            and manifest.get("env_hash") != sandbox_workload_environment_hash(
                execution_sandbox.environment_hash, gpu_required)):
        raise ManifestError(
            "manifest.env_hash 与本次 CPU/GPU workload sandbox identity 不一致")
    if (gpu_required
            and int(policy.get("resources", {}).get("gpus", 0)) <= 0):
        raise ManifestError(
            "manifest 要求 GPU，但 policy.resources.gpus 未声明可用 allocation")
    if (gpu_required and (execution_sandbox is None
                          or getattr(execution_sandbox, "gpu_contract", None) is None)):
        raise ManifestError(
            "manifest 要求 GPU，但启动 canary 未证明 fixed sandbox allocation")
    rc = resolve_command(manifest, kind, src_dir=src_dir, work_root=work_root, policy=policy,
                         ckpt_path=ckpt_path, ckpt_content_hash=ckpt_content_hash,
                         expected_source_hashes=expected_source_hashes,
                         allowed_asset_refs=allowed_asset_refs,
                         expected_asset_identities=expected_asset_identities)
    sandbox_invocation = None
    try:
        run_argv = rc.argv
        run_env = rc.env or None
        run_pass_fds = rc.pass_fds
        if execution_sandbox is not None:
            sandbox_context = dict(execution_context or {})
            if ("log_name" in sandbox_context
                    and sandbox_context["log_name"] != log_name):
                raise ManifestError("execution_context.log_name 与 manifest log_name 冲突")
            sandbox_context["log_name"] = log_name
            sandbox_invocation = execution_sandbox.prepare(
                rc.argv, staging_dir=staging_dir, log_name=log_name,
                env=rc.env or None, timeout_s=rc.timeout_s,
                fd_expectations=rc.fd_expectations,
                tree_expectations=rc.tree_expectations,
                execution_context=sandbox_context,
                execution_supervisor=execution_supervisor,
                gpu_required=gpu_required)
            run_argv = sandbox_invocation.argv
            run_env = sandbox_invocation.env
            run_pass_fds = sandbox_invocation.pass_fds
        result = H.run_staged(
            run_argv, staging_dir=staging_dir, log_name=log_name,
            timeout_s=rc.timeout_s, env=run_env, pass_fds=run_pass_fds,
            execution_supervisor=execution_supervisor,
            execution_kind=f"manifest-{kind}",
            execution_context=execution_context,
            sandbox_invocation=sandbox_invocation)
        try:
            for fd, content_hash, size, device, inode in rc.fd_expectations:
                verify_open_fd(
                    fd, expected_hash=content_hash, expected_size=size,
                    expected_device=device, expected_inode=inode)
            for fd, hashes, allowed_extra in rc.tree_expectations:
                verify_tree_fd(
                    fd, hashes, label="bundle source tree post-use",
                    exact=True, allowed_extra=allowed_extra)
        except ArtifactCapabilityError as error:
            raise ManifestError(str(error)) from error
        return result
    finally:
        if sandbox_invocation is not None:
            # run_staged closes it on every ordinary execution path; this
            # idempotent close also covers validation/pointer failures that
            # occur before run_staged reaches its lifecycle cleanup block.
            sandbox_invocation.close()
        for fd in set(rc.pass_fds):
            try:
                os.close(fd)
            except OSError:
                pass

"""console_server —— 人类控制台数据面（步⑨ CP9.1）：把真运行库投影成**控制台前端消费形状**的 JSON
（CP9.2 前端据此由原型 v2 改造换数据源），并收人工入站消息到 spool 文件。

**形状说明**：本模块产的是 server-canonical 形状（真表投影统一嵌 `payload["tables"][<表名>]`、派生对象
[status_card/live/notification/policy/ledger_by_cycle/fs] 平铺顶层）——**非**原型 mock 的顶层 `DB.<表名>`
形状；CP9.2 前端 loader 负责这层适配（换数据源手术），故本模块不必逐字对齐原型 mock 的键路径。

**单写纪律铁律（§6.6）**：本服务是**独立进程**，对研究库**只读**（`mode=ro` 物理只读，SQLite 拒一切写；
见 _open_ro——控制台是人类全量观测面须读全表+PRAGMA，故用 mode=ro 而非 grounding 应答器的裁剪连接），
入站消息只**追加写 inbox spool 文件**（非 DB）——run 进程在 precheck 边界 ingest 该 spool 走 M5 既有链
（InteractionIngest→Console→Mediator）。控制台**永不写 DB**，故不破坏「WriteDaemon 单写者」。

**为什么动态投影**：DDL 是冻结件（36 表三重锁）。本模块用 `PRAGMA table_info` 取真列名投影每张表为
list[dict]——不硬编码列名，DDL 不变则形状稳定；前端（原型派生）按需读字段、缺的显示空。派生对象
（status_card / live / notification / policy / FS 树）单独组装。

**零新依赖**：stdlib http.server + sqlite3 + json + yaml（已依赖）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import yaml

# 控制台投影的表清单（真 DDL 表名；import/license 等当前无数据则投影空数组，前端照渲染）。
_PROJECT_TABLES = [
    "goal", "question", "question_dep", "cycle", "baseline", "baseline_tag", "variant", "run",
    "protocol", "metric_def", "protocol_metric", "evaluation", "evaluation_attempt", "metric_result",
    "answer", "answer_applicability", "evidence", "decision", "directive", "build_target",
    "build_target_required_metric", "checkpoint", "execution_log", "execution_observation",
    "external_candidate", "license_review", "external_import",
    "interaction_message", "interaction_classification", "interaction_reply", "interaction_request",
    "ledger", "runner_call", "phase_commit",
]


def _open_ro(db_path: str) -> sqlite3.Connection:
    """控制台专用只读连接：**mode=ro**（物理只读，写操作被 SQLite 拒——单写者纪律的硬保证）。
    不用 mediator.open_responder_read_conn（其 authorizer 为 grounding 应答器裁剪、连 PRAGMA/观测表都拒），
    因控制台是**人类全量观测面**、须读得到所有表 + PRAGMA 取列名；mode=ro 已足够保证零写。"""
    conn = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)
    conn.isolation_level = None
    return conn


# 高基数表投影上限（内审 NIT：execution_log/ledger/runner_call 长跑会涨大；execution_log 是指针表[ref+hash，
# 不含大 BLOB]故行宽有界，但行数无界——投影取最新 _CAP 行，防整表拉进单个 JSON 拖垮前端）。其余表规模有界。
_ROW_CAP = 500
_CAPPED = frozenset({"execution_log", "execution_observation", "ledger", "runner_call", "decision",
                     "metric_result", "evaluation_attempt", "run"})


def _rows(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    """动态列投影：PRAGMA 取真列名 → SELECT * → list[dict]。表不存在（旧库）→ 空。高基数表取最新 _ROW_CAP 行。"""
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.DatabaseError:
        return []
    if not cols:
        return []
    # 有 id 主键的高基数表按 id DESC 取最新 CAP 行（无 pagination——观测面裁量，控制台看最近态）
    sql = f"SELECT * FROM {table}"
    if table in _CAPPED and "id" in cols:
        sql += f" ORDER BY id DESC LIMIT {_ROW_CAP}"
    elif table in _CAPPED:
        sql += f" LIMIT {_ROW_CAP}"
    return [dict(zip(cols, row)) for row in conn.execute(sql).fetchall()]


def _load_status_card(work_root: Path) -> Optional[Dict[str, Any]]:
    """读发布产物 <work>/state/status_card.json（advancer 阶段边界原子发布；无=尚未发布过）。"""
    p = work_root / "state" / "status_card.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _notifications(work_root: Path) -> List[Dict[str, Any]]:
    """读 outbox 事件队列（notify.Outbox 落的每行 JSON；committed=换行终止，撕裂尾行忽略）。"""
    p = work_root / "state" / "outbox.jsonl"
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if lines and not text.endswith("\n"):
        lines = lines[:-1]                 # 尾行无换行 = 未 committed（append 中途撕裂）→ 丢，即便它恰是合法
    out = []                               # JSON 前缀也不当已发事件（committed=换行终止，同 outbox 纪律，codex SHOULD）
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                       # 中段坏行忽略（只读观测面，不 fail loud）
    return out


def _live(conn: sqlite3.Connection, work_root: Path) -> Dict[str, Any]:
    """「正在执行」活性信号（不经任何 LLM，§4.6.6 live strip）：在途轮 + 最新 runner_call + 心跳
    （transcript 文件 mtime）+ 模式（running / idle / awaiting_user）。"""
    inflight = conn.execute(
        "SELECT id, route, status, active_question_id FROM cycle "
        "WHERE status NOT IN ('done','aborted','failed') ORDER BY id DESC LIMIT 1").fetchone()
    pending_req = conn.execute("SELECT id FROM interaction_request WHERE status='pending' LIMIT 1").fetchone()
    rc = conn.execute("SELECT id, cycle_id, phase, purpose, status, transcript_ref, started_at, finished_at "
                      "FROM runner_call ORDER BY id DESC LIMIT 1").fetchone()
    mode = "awaiting_user" if pending_req else ("running" if inflight else "idle")
    live: Dict[str, Any] = {"mode": mode,
                            "inflight_cycle": (f"c{inflight[0]}" if inflight else None),
                            "inflight_route": (inflight[1] if inflight else None),
                            "inflight_status": (inflight[2] if inflight else None),
                            "pending_request_id": (pending_req[0] if pending_req else None),
                            "runner_call": None, "heartbeat_age_s": None}
    if rc is not None:
        live["runner_call"] = {"id": rc[0], "cycle_id": rc[1], "phase": rc[2], "purpose": rc[3],
                               "status": rc[4], "transcript_ref": rc[5],
                               "started_at": rc[6], "finished_at": rc[7]}
        if rc[5]:                          # 心跳 = transcript 文件 mtime 相对现在的**年龄秒**（真活性；文件不在则 None）
            tp = work_root / rc[5]
            if tp.exists():
                try:
                    live["heartbeat_age_s"] = round(time.time() - tp.stat().st_mtime, 1)  # 内审 SHOULD：单键单义（年龄非绝对 mtime）
                except OSError:
                    pass
    return live


def _ledger_by_cycle(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [{"cycle": f"c{r[0]}", "money": r[1]} for r in conn.execute(
        "SELECT cycle_id, COALESCE(SUM(money),0) FROM ledger GROUP BY cycle_id ORDER BY cycle_id").fetchall()]


def _fs_tree(work_root: Path, system_root: Path) -> Dict[str, Any]:
    """真文件树（控制台文件浏览器）：work_root 运行产物 + system_root 的 schemas/prompts/policies（只读展示）。
    深度/条目有界（防超大树拖垮前端）；每节点 {p: 相对名, dir: bool, size: 字节}。"""
    def walk(base: Path, rel: str, depth: int) -> List[Dict[str, Any]]:
        if depth > 6:
            return []
        try:
            entries = sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name))[:200]
        except OSError:
            return []
        nodes = []
        for e in entries:
            if e.name.startswith(".") or e.is_symlink():
                continue
            node = {"p": e.name, "dir": e.is_dir()}
            if e.is_dir():
                node["children"] = walk(e, f"{rel}/{e.name}", depth + 1)
            else:
                try:
                    node["size"] = e.stat().st_size
                except OSError:
                    node["size"] = None
            nodes.append(node)
        return nodes
    roots = [{"p": "work", "dir": True, "children": walk(work_root, "work", 0)}]
    for sub in ("schemas", "prompts", "policies", "input"):
        d = system_root / sub
        if d.exists():
            roots.append({"p": sub, "dir": True, "children": walk(d, sub, 0)})
    return {"roots": roots}


def assemble_db(db_path: str, work_root: str, system_root: str) -> Dict[str, Any]:
    """组装控制台 /api/db 载荷（纯函数、只读连接、可单测）：真表投影 + 派生对象。"""
    conn = _open_ro(db_path)
    try:
        payload: Dict[str, Any] = {"tables": {t: _rows(conn, t) for t in _PROJECT_TABLES}}
        payload["status_card"] = _load_status_card(Path(work_root))
        payload["live"] = _live(conn, Path(work_root))
        payload["notification"] = _notifications(Path(work_root))
        payload["ledger_by_cycle"] = _ledger_by_cycle(conn)
    finally:
        conn.close()
    pol = system_root and (Path(system_root) / "policies" / "policy.yaml")
    try:                                   # policy 解析失败**不拖垮整个仪表盘**（内审 SHOULD：与其余 reader
        payload["policy"] = yaml.safe_load(pol.read_text(encoding="utf-8")) if pol and pol.exists() else {}
    except (yaml.YAMLError, OSError):      # 一致的 degrade-gracefully 姿态——坏配置只让 policy 面空、不 500）
        payload["policy"] = {}
    payload["fs"] = _fs_tree(Path(work_root), Path(system_root))
    return payload


class ConsoleData:
    """控制台数据源 + 入站 spool（不含 HTTP；供 handler 与测试共用）。只读库 + 只写 inbox spool。"""

    def __init__(self, *, db_path: str, work_root: str, system_root: str):
        self.db_path = db_path
        self.work_root = Path(work_root)
        self.system_root = Path(system_root)
        self.inbox = self.work_root / "state" / "console_inbox.jsonl"
        self._inbox_lock = threading.Lock()   # ThreadingHTTPServer 并发 POST → seq 分配须串行（内审 SHOULD）

    def db(self) -> Dict[str, Any]:
        return assemble_db(self.db_path, str(self.work_root), str(self.system_root))

    def _virtual_root(self, seg: str) -> Optional[Path]:
        """FS 树暴露的虚拟根 → 真目录（显式映射，不靠 base.parent 猜——codex SHOULD：--work-root 叫任意名
        时 base.parent/rel 拼法会 404）。work→work_root；schemas/prompts/policies/input→system_root/<seg>。"""
        if seg == "work":
            return self.work_root
        if seg in ("schemas", "prompts", "policies", "input"):
            return self.system_root / seg
        return None

    def read_file(self, rel: str) -> Optional[bytes]:
        """白名单读：路径按**虚拟根**（FS 树暴露的 work/schemas/prompts/policies/input）显式解析到真目录 →
        resolve() + containment 防逃逸（symlink 解析后判、绝对路径 lstrip 化相对）。目录/越界/不存在 → None。"""
        parts = rel.lstrip("/").split("/", 1)
        base = self._virtual_root(parts[0])
        if base is None:
            return None
        sub = parts[1] if len(parts) > 1 else ""
        if ".." in sub.split("/"):
            return None
        try:
            resolved = (base / sub).resolve()
            root = base.resolve()
            if (resolved == root or root in resolved.parents) and resolved.is_file():
                return resolved.read_bytes()
        except OSError:
            pass
        return None

    def enqueue_message(self, text: str, connector: str = "console") -> Dict[str, Any]:
        """人工入站 → 追加写 inbox spool（一行 JSON；run 进程 precheck 边界 ingest）。**不写 DB**。
        每次提交一条独立记录（seq 单调递增、idempotency_key 唯一——运维每次点击=一条 intent；M5 ingest 按
        UNIQUE(connector,idempotency_key) 去重同一条的重放）。seq 分配 + 追加**在锁内串行**（内审 SHOULD：
        ThreadingHTTPServer 并发 POST 下 read-then-append 竞态会撞 seq）。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("空消息")
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self._inbox_lock:                 # 锁仅进程内串行 seq——**单 console_server 实例假设**：多实例会分配重复
            #                                    idempotency_key(console-{seq})，被 ingest 幂等层(interaction_message UNIQUE)
            #                                    当重放吞掉第二条。运维部署须保证单实例（同 run 单写纪律）。
            seq = 1 + sum(1 for _ in self.inbox.open(encoding="utf-8")) if self.inbox.exists() else 1
            rec = {"connector": connector, "raw_text": text, "seq": seq,
                   "idempotency_key": f"console-{seq}"}
            with self.inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")   # 换行终止 = committed（同 outbox 纪律）
        return rec


def make_handler(data: ConsoleData, static_dir: Optional[Path]):
    """构造 HTTP handler 类（闭包持 data + 静态目录）。路由：
    GET /api/db · GET /api/file?p=… · POST /api/message{text} · GET /（静态控制台页）。"""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):        # 静默（观测服务，不刷屏）
            pass

        def _json(self, code: int, obj: Any):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/db":
                try:
                    self._json(200, data.db())
                except Exception:                     # 只读观测面：组装失败向客户端**泛化报**（不泄内部细节/路径，
                    import traceback                   # codex SHOULD）；真实细节写 stderr 供运维排障（codex 第2轮
                    traceback.print_exc()              # NIT：文案承诺「详见服务端日志」须真有日志，否则线上排障盲）
                    self._json(500, {"error": "内部错误：/api/db 组装失败（详见服务端日志）"})
                return
            if u.path == "/api/file":
                from urllib.parse import parse_qs
                rel = (parse_qs(u.query).get("p") or [""])[0]
                content = data.read_file(rel)
                if content is None:
                    self._json(404, {"error": f"文件不可读/不在白名单: {rel}"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self._serve_static(u.path)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/message":
                n = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    # connector 固定 "console"（codex NIT：不许客户端伪造成其他来源；控制台入口即 console）
                    rec = data.enqueue_message(body.get("text", ""))
                    self._json(200, {"ok": True, "queued": rec})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                return
            self._json(404, {"error": "未知路由"})

        def _serve_static(self, path: str):
            if static_dir is None:
                self._json(404, {"error": "未配静态目录"})
                return
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            if ".." in rel.split("/"):
                self._json(403, {"error": "路径越界"})
                return
            # resolve + containment（codex SHOULD：static_dir 内 symlink 不得跟出目录——与 read_file 同套）
            try:
                fp = (static_dir / rel).resolve()
                sroot = static_dir.resolve()
            except OSError:
                self._json(404, {"error": f"不存在: {rel}"})
                return
            if not ((fp == sroot or sroot in fp.parents) and fp.is_file()):
                self._json(404, {"error": f"不存在: {rel}"})
                return
            ctype = "text/html; charset=utf-8" if fp.suffix in (".html", ".htm") else \
                    "application/javascript" if fp.suffix == ".js" else \
                    "text/css" if fp.suffix == ".css" else "application/octet-stream"
            body = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def serve(db_path: str, work_root: str, system_root: str, *, host: str = "127.0.0.1", port: int = 8765,
          static_dir: Optional[str] = None) -> ThreadingHTTPServer:
    """起控制台服务（阻塞前调用方 serve_forever）。static_dir=控制台前端目录（默认 system_root/views/console）。"""
    sd = Path(static_dir) if static_dir else (Path(system_root) / "views" / "console")
    data = ConsoleData(db_path=db_path, work_root=work_root, system_root=system_root)
    httpd = ThreadingHTTPServer((host, port), make_handler(data, sd if sd.exists() else None))
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="meta-research 人类控制台数据面服务（只读 + spool 入站；步⑨）")
    ap.add_argument("--system-root", required=True, help="仓库根（含 policies/schemas/prompts/views）")
    ap.add_argument("--work-root", required=True, help="运行产物根（research.sqlite / state 落此，同 run.py）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    db_path = str(Path(args.work_root) / "research.sqlite")
    httpd = serve(db_path, args.work_root, args.system_root, host=args.host, port=args.port)
    print(f"[console] 数据面 http://{args.host}:{args.port}  （只读库 {db_path}；Ctrl-C 停）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

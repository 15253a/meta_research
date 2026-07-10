"""CP9.2 · 控制台前端接入（views/console/index.html 由原型派生，换数据源）。

验收：①前后端形状契约——assemble_db 产的键 ⊇ 前端 adaptPayload 消费的（server/前端不漂移）；
②前端整合手术就位（let DB / adaptPayload / refreshDB / /api/db·/api/message·/api/file / 原型渲染码保真）；
③node HEADLESS 冒烟——改后页在真数据形状上加载 + render() 不抛 + adaptPayload 映射回原型 DB 形状。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import conftest
from orchestrator import console_server as CS
from orchestrator import database as db

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
PAGE = SYSTEM_ROOT / "views" / "console" / "index.html"
SMOKE = SYSTEM_ROOT / "tests" / "console_smoke.js"
AUTH_SMOKE = SYSTEM_ROOT / "tests" / "console_auth_smoke.js"


def _payload(tmp_path, *, seed=True, status_card=True) -> dict:
    work = tmp_path / "work"; (work / "state").mkdir(parents=True)
    path = str(work / "research.sqlite")
    conn = db.connect(path)
    if seed:
        conftest.seed_minimal(conn)
    conn.commit(); conn.close()
    if status_card:                                              # 真 status_card 是嵌套（goal/selection/counts/budget）——测拍平 + budget 合成
        (work / "state" / "status_card.json").write_text(json.dumps({
            "snapshot_cycle": 3, "goal": {"id": 1, "ver": 2, "summary": "长上下文外推"},
            "active_question": {"id": "q13", "text": "gla-gate"}, "cycle_status": "bundle", "route": "attack",
            "selection": {"intent": "attack"}, "budget": {"B_t": 40, "cycle_spent": 13.7, "global_remaining": 514.4},
            "counts": {"open": 3, "inconclusive": 0}, "pending_file_request": None}), encoding="utf-8")
    return CS.assemble_db(path, str(work), str(SYSTEM_ROOT))


# ============ 前后端形状契约（无 node 也能跑）============
def test_page_derived_from_prototype_and_wired():
    """前端由原型派生（渲染码保真）+ 换数据源手术就位。"""
    txt = PAGE.read_text(encoding="utf-8")
    assert "function render(){ renderTopbar(); renderTabs(); renderStage(); }" in txt   # 原型渲染码保真
    assert "let DB = {" in txt and "const DB = {" not in txt                             # DB 改可重赋值
    for marker in ("function adaptPayload(", "async function refreshDB(", "/api/db",
                   "/api/message", "/api/directive", "function directiveAction(",
                   "/api/file-request", "function fileRequestAction(",
                   "/api/file", "setInterval(refreshDB"):
        assert marker in txt, f"前端缺整合标记: {marker}"
    assert "function apiFetch(" in txt and "bootstrapConsoleCapability()" in txt
    assert "function apiPost(" in txt and "Idempotency-Key" in txt
    assert "sessionStorage" in txt and "history.replaceState" in txt
    assert "localStorage" not in txt and "document.cookie" not in txt
    assert "Authorization" in txt and 'credentials:"omit"' in txt
    assert 'get("demo")==="1"' in txt and "showConnectionGuard" in txt and "_consoleReady" in txt
    assert "mock 只在显式 ?demo=1" in txt
    assert 'showConnectionGuard(_consoleCapability?"正在连接真实控制台…":"需要 capability' in txt
    assert txt.count("if(!_consoleReady)") >= 2 and "if(!DEMO_MODE && !_consoleReady)" in txt
    assert "Object.keys(pending).length>=64" in txt and "绝不淘汰未决请求" in txt
    assert "fetch('/api" not in txt and 'fetch("/api' not in txt  # 所有 API 必须统一经过 capability 包装器
    live_runtime = txt[txt.index('const HEADLESS = (typeof window === "undefined")'):]
    for fake_runtime_fact in ("qq:7742", "p95 1.1s", "p95 1.6s", "m.ack_s", "rep.grounding_ok"):
        assert fake_runtime_fact not in live_runtime             # 显式 demo 可保留原型；live 运行段不得引用其事实
    assert "未采集 p95" in txt and "grounding 写入前检查" in txt
    assert "const chip = (cls,txt,dot)" in txt and "+esc(txt)+" in txt  # DB 标签不能借 chip 注入同源脚本窃 token
    assert "+e.source+" not in txt and "${lr.actor}" not in txt
    assert 'data-fs-action="toggle"' in txt and 'data-fs-path="${esc(path)}"' in txt
    assert "onclick=\"fsToggle('${path}')\"" not in txt               # 上传文件名不得进入 inline JS（stored XSS）


def test_adaptpayload_contract_keys(tmp_path):
    """契约：assemble_db 产的顶层键覆盖 adaptPayload 消费的（tables + 派生对象），防 server/前端漂移。"""
    p = _payload(tmp_path)
    assert {"tables", "status_card", "live", "notification", "ledger_by_cycle", "policy", "fs"} <= set(p)
    # adaptPayload 平铺 tables → 顶层；这些表 render 会读
    assert {"question", "cycle", "baseline", "variant", "decision", "build_target",
            "evaluation", "metric_result", "answer", "directive"} <= set(p["tables"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_browser_capability_flow():
    """capability/fail-closed 遮罩 + POST 幂等键保留、回显清理、容量上限的浏览器语义。"""
    r = subprocess.run(["node", str(AUTH_SMOKE), str(PAGE)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "AUTH_SMOKE_OK" in r.stdout, f"stdout={r.stdout} stderr={r.stderr[:400]}"


# ============ node HEADLESS 冒烟（有 node 才跑）============
@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_headless_render_and_adapt(tmp_path):
    """改后页在真数据形状上：adaptPayload 映射 + applyLive 真 live 覆盖 + clampSelections + 逐个渲染全 9 标签页均不抛。
    这是"接入真数据不崩"的主钉：原型把 mock 焊进渲染码（budget/status_card/live/事件流/leaderboard），冒烟遍历全标签页兜住。"""
    pf = tmp_path / "payload.json"
    pf.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")
    r = subprocess.run(["node", str(SMOKE), str(PAGE), str(pf)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "SMOKE_OK" in r.stdout, f"stdout={r.stdout} stderr={r.stderr[:400]}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_headless_render_empty_db(tmp_path):
    """空库（全新 run：零行 + 无 status_card）也须逐页不崩——真实开机态；夯实空表/缺选中/null 的诚实占位与守卫。"""
    pf = tmp_path / "empty.json"
    pf.write_text(json.dumps(_payload(tmp_path, seed=False, status_card=False)), encoding="utf-8")
    r = subprocess.run(["node", str(SMOKE), str(PAGE), str(pf)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "SMOKE_OK" in r.stdout, f"stdout={r.stdout} stderr={r.stderr[:400]}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_untrusted_db_and_status_strings_do_not_reach_innerhtml(tmp_path):
    """页面持有 Bearer；DB/status/policy 全部展示面的存储型字符串不得成为同源脚本。"""
    payload = _payload(tmp_path)
    attack = '<svg data-xss="sentinel" onload="fetch(`/api/directive`)">'
    payload["status_card"]["active_question"] = attack
    payload["status_card"]["budget"].update(
        {"B_t": attack, "cycle_spent": attack, "global_remaining": attack}
    )
    payload["policy"]["budget"].update(
        {"B0": attack, "doubling_period_m": attack, "B_max": attack, "session_max": attack}
    )
    baseline = payload["tables"]["baseline"][0]
    payload["tables"]["baseline_tag"] = [{"baseline_id": baseline["id"], "tag": attack}]
    payload["tables"]["variant"][0]["env_hash"] = attack
    payload["tables"]["evaluation_attempt"][0]["env_hash"] = attack
    payload["tables"]["cycle"][0]["started_at"] = attack
    payload["tables"]["evidence"][0]["claim_md"] = attack
    payload["tables"]["external_candidate"] = [{
        "id": 9001, "rank": 1, "canonical_uri": "https://example.invalid/" + attack,
        "revision": attack, "trigger_kind": attack, "search_snapshot_hash": attack,
        "trigger_snapshot_hash": attack, "license_id_seen": attack,
    }]
    payload["tables"]["license_review"] = [{
        "candidate_id": 9001, "decision": "allow", "actor": attack,
        "note": attack, "scope": None,
    }]
    payload["tables"]["external_import"] = [{
        "candidate_id": 9001, "action_cycle": 1, "action": "selected",
        "candidate_set_hash": attack, "selection_key": attack, "policy_hash": attack,
        "license_decision_snapshot_hash": attack, "manifest_hash": None, "baseline_id": None,
    }]
    pf = tmp_path / "xss-payload.json"
    pf.write_text(json.dumps(payload), encoding="utf-8")
    r = subprocess.run(
        ["node", str(SMOKE), str(PAGE), str(pf), '<svg data-xss='],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "SMOKE_OK" in r.stdout, f"stdout={r.stdout} stderr={r.stderr[:400]}"

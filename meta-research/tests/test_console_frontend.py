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
                   "/api/message", "/api/file", "setInterval(refreshDB"):
        assert marker in txt, f"前端缺整合标记: {marker}"


def test_adaptpayload_contract_keys(tmp_path):
    """契约：assemble_db 产的顶层键覆盖 adaptPayload 消费的（tables + 派生对象），防 server/前端漂移。"""
    p = _payload(tmp_path)
    assert {"tables", "status_card", "live", "notification", "ledger_by_cycle", "policy", "fs"} <= set(p)
    # adaptPayload 平铺 tables → 顶层；这些表 render 会读
    assert {"question", "cycle", "baseline", "variant", "decision", "build_target",
            "evaluation", "metric_result", "answer", "directive"} <= set(p["tables"])


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

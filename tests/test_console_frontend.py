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
    assert "function render(options){" in txt and "renderTopbar();renderTabs();" in txt
    assert "function refreshOpenQuestionTree(" in txt and "refreshQuestionDrawer(scrollSnapshot)" in txt
    assert "let DB = {" in txt and "const DB = {" not in txt                             # DB 改可重赋值
    for marker in ("function adaptPayload(", "async function refreshDB(", "/api/db",
                   "/api/message", "/api/directive", "function directiveAction(",
                   "/api/file-request", "function fileRequestAction(",
                   "/api/quests", "function createQuest(", "id=\"quest-select\"",
                   "/api/setup", "/api/quest-drafts", "function publishQuestDraft(",
                   "/api/quest-control", "/api/quest-runtime?quest=",
                   "/api/file-request-uploads", "function uploadFileRequest(",
                   "/api/query", "function syncNarratorReplies(",
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
    assert 'id="console-empty-state"' in txt and 'onclick="createQuest()">新建研究任务' in txt
    assert "function showEmptyRegistryState()" in txt and "function emptyRegistrySnapshotIsFresh()" in txt
    quest_refresh = txt[txt.index("async function refreshQuests("):txt.index("function selectQuest(")]
    assert "'/api/quests?view=selector'" in quest_refresh
    assert 'else showEmptyRegistryState();' in quest_refresh
    assert "const inFlight=_questRegistryInFlight,result=await inFlight;" in quest_refresh
    assert "if(preferred) return await refreshQuests(preferred);" in quest_refresh
    assert "selectionGeneration!==_questSelectionGeneration" in quest_refresh
    assert "markConsoleSnapshotFresh()" not in quest_refresh  # 空 registry 不能冒充 quest DB 快照
    db_refresh = txt[txt.index("async function refreshDB("):txt.index('if(typeof window!=="undefined"', txt.index("async function refreshDB("))]
    assert "if(MULTI_QUEST_MODE && !ACTIVE_QUEST_ID)" in db_refresh and "await refreshQuests();" in db_refresh
    assert txt.count("if(!_consoleReady)") >= 2 and "if(!DEMO_MODE && !_consoleReady)" in txt
    assert "Object.keys(pending).length>=64" in txt and "绝不淘汰未决请求" in txt
    assert "action_target" not in txt[txt.index("sendNarr=function"):txt.index("renderShell();")]
    assert "apiPost('/api/query'" in txt and "quest_id:ACTIVE_QUEST_ID" in txt
    narrator_sync = txt[txt.index("function syncNarratorReplies("):txt.index("function narrCopy(")]
    narrator_send = txt[txt.index("sendNarr=function"):txt.index("renderShell();")]
    assert "function _narratorProgressState(" in txt and 'role="status" aria-live="polite"' in txt
    assert 'call.phase==="interaction_query"' in txt and 'calls.get("message:"+message.id)' in narrator_sync
    assert "message.idempotency_key" in narrator_sync and "item.idempotencyKey===idempotencyKey" in narrator_sync
    assert ".indexOf(message.raw_text)" not in narrator_sync
    assert "onPrepared" in txt[txt.index("async function apiPost("):txt.index("async function apiRegistryPost(")]
    assert "ACTIVE_QUEST_ID!==questId" in narrator_send and "refreshDB();" in narrator_send
    for status_text in ("正在提交问题", "问题已入队", "正在启动 Codex", "Codex 正在生成回答", "回答失败"):
        assert status_text in txt
    assert "fetch('/api" not in txt and 'fetch("/api' not in txt  # 所有 API 必须统一经过 capability 包装器
    live_runtime = txt[txt.index('const HEADLESS = (typeof window === "undefined")'):]
    for fake_runtime_fact in ("qq:7742", "p95 1.1s", "p95 1.6s", "m.ack_s", "rep.grounding_ok"):
        assert fake_runtime_fact not in live_runtime             # 显式 demo 可保留原型；live 运行段不得引用其事实
    assert "未采集 p95" in txt and "grounding 写入前检查" in txt
    assert "const chip = (cls,txt,dot)" in txt and "+esc(txt)+" in txt  # DB 标签不能借 chip 注入同源脚本窃 token
    assert "+e.source+" not in txt and "${lr.actor}" not in txt
    assert 'data-fs-action="toggle"' in txt and 'data-fs-path="${esc(path)}"' in txt
    assert "onclick=\"fsToggle('${path}')\"" not in txt               # 上传文件名不得进入 inline JS（stored XSS）
    assert 'Q_EXEC_PHASES=new Set([' in txt and "Q_EXEC_PHASES.map" not in txt
    assert "function selectQuestionPhase(" in txt and "function _questionPhaseOutput(" in txt
    assert "实际输出" in txt and 'data-live-output="1"' in txt
    assert "function _legacyLiveActivityRows(" in txt and "function syncCodexLiveChip(" in txt
    assert 'event.kind==="activity"' in txt and 'event.kind==="live"' in txt
    assert 'id="codex-live-chip"' in txt and "Codex 实时执行" in txt
    assert 'class="qactivity-list"' in txt and "查看持续更新的原始可见输出" in txt
    assert "这一轮没有单独调用 Codex" in txt
    assert 'render({preserveScroll:true})' in txt
    assert "function _captureRenderScroll()" in txt and "function _restoreRenderScroll(" in txt
    assert 'RENDER_SCROLL_CONTAINER_IDS=["stage-page","fstree","stream","narrlog"]' in txt
    assert 'data-scroll-key="runner-output-${outputId}"' in txt
    assert "_resetConsoleViewScroll();" in txt[txt.index("function selectQuest("):txt.index("function _setupRows(")]


def test_web_first_quest_and_file_request_upload_contract():
    """浏览器文件走受管分块；单机已有目录显式只读接入，不要求用户写 YAML。"""
    txt = PAGE.read_text(encoding="utf-8")
    assert 'id="quest-wizard"' in txt and 'aria-labelledby="quest-wizard-title"' in txt
    assert 'class="modalfoot wizardfoot"' in txt
    common_status = txt.index('id="quest-upload-status"')
    assert common_status > txt.index('data-quest-step="3"') and common_status < txt.index('id="quest-wizard-cancel"')
    assert 'id="file-request-upload-dialog"' in txt
    assert txt.count("webkitdirectory") >= 2 and txt.count('type="file" multiple') >= 4
    assert "file.webkitRelativePath || file.name" in txt
    assert "file.path" not in txt and "file.arrayBuffer()" not in txt
    assert "UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024" in txt
    assert "class IncrementalSHA256" in txt and "file.slice(offset,end).arrayBuffer()" in txt
    assert '"&offset="+offset+"&sha256="' in txt
    assert 'headers:{"Content-Type":"application/octet-stream","Idempotency-Key":pending.key}' in txt
    assert "for(let attempt=0;attempt<attempts;attempt++)" in txt
    assert 'body.idempotency_key==="console-"+key' in txt

    create = txt[txt.index("async function createQuest("):txt.index("function closeQuestWizard(")]
    assert "window.prompt" not in create and "showModal()" in create
    assert "buildCustomGoalBrief" in txt and 'predicate_json: "+JSON.stringify(predicate)' in txt
    assert "粘贴完整 goal_brief" not in txt
    assert "[hidden]{display:none!important}" in txt  # author display:flex 不得覆盖原生 hidden
    assert 'id="quest-qualification-field" hidden' in txt
    assert "密封一次性评测（仅高级 T1）" in txt and "资格配置" not in txt
    assert "使用预设研究方案" in txt and "从空白自定义" in txt
    assert 'id="quest-template-description"' in txt and 'templateRow.category' in txt
    assert "profileField.hidden=custom||compatible.length===0" in txt
    assert "function questTemplateChanged(" in txt and "row.template_id===templateId" in txt

    assert 'id="quest-local-datasets"' in txt and 'id="quest-local-references"' in txt
    assert "每行一个" in txt and "无需经浏览器搬运大文件" in txt
    assert '"/api/quest-drafts/local-sources",{draft_id:_questWizard.draftId,kind:entry.kind,path:entry.path}' in txt
    assert "function _parseLocalSourceText(" in txt and 'path.startsWith("/")' in txt
    assert "return {label:label,count:count,bytes:bytes}" in txt
    assert "candidate.path" not in txt and "result.body.path" not in txt

    request = txt[txt.index("function fileRequestAction("):txt.index("function provideR2(")]
    assert "uploadFileRequest(id)" in request
    assert "source_ref" not in request and "已放好文件的虚拟目录" not in request
    assert 'apiPost(\'/api/file-request\',{action:action' in request
    assert 'action==="approve"' in request and "_permissionRequest(request)" in request
    assert "权限确认只显示“同意 / 不同意”" in txt
    assert 'Object.assign({},identityFields,{path:path,size:file.size})' in txt
    assert 'quest_id:identity.quest_id,request_id:identity.request_id,upload_id:identity.upload_id' in txt
    assert '"quest_id="+encodeURIComponent(identity.quest_id)+"&request_id="' in txt
    assert '"/api/file-request-uploads",{quest_id:_fileRequestUpload.questId,request_id:_fileRequestUpload.requestId}' in txt
    assert '"/api/file-request-uploads/publish",{quest_id:_fileRequestUpload.questId,request_id:_fileRequestUpload.requestId,upload_id:_fileRequestUpload.uploadId}' in txt
    assert 'oncancel="if(_fileRequestUpload.busy) event.preventDefault()"' in txt
    assert "work/uploads" not in txt and "input/uploads" not in txt and "从 uploads 解决" not in txt

    assert '"/api/quest-drafts/publish",publishPayload' in txt
    assert '"/api/quest-publish-status?job="' in txt
    publish = txt[txt.index("async function publishQuestDraft("):txt.index("function _setRuntimeProfileEditorBusy(")]
    assert "publishPolls+=1" in publish
    assert "首次完整性校验" in publish and "已确认后台仍在运行" in publish
    assert "function _formatByteCount(" in txt
    assert "selectQuest(questId);" in publish
    assert "refreshQuests(questId)" in publish
    assert "await refreshQuests();selectQuest(questId)" not in publish
    assert "'/api/quests?view=selector'" in txt
    assert '"/api/quest-control",{quest_id:questId,action:action}' in txt
    assert 'if(HEADLESS || DEMO_MODE) return;' in create

    assert 'id="quest-runtime-log-dialog"' in txt and "查看诊断" in txt
    assert 'id="deployment-health"' in txt and "密封评测：已部署" in txt
    assert "可选 sealed 评测" not in txt
    runtime_log = txt[txt.index("async function showQuestRuntimeLog("):txt.index("async function refreshQuestRuntime(")]
    assert '"/api/quest-runtime-log?quest="+encodeURIComponent(questId)' in runtime_log
    assert "diagnostic.text" in runtime_log and "log_ref" not in txt and "web-owner.log" not in txt
    assert "if(HEADLESS || DEMO_MODE || !ACTIVE_QUEST_ID) return;" in runtime_log
    assert 'const _startupPending = new Set();' in txt
    assert 'showQuestRuntimeLog(questId)' in txt
    assert '任务已创建并启动' not in txt


def test_runtime_profile_creation_and_live_edit_contract():
    """新建与运行中编辑共用 v1/v2/v3 配置，并只在耐久轮次安全重启后应用。"""
    txt = PAGE.read_text(encoding="utf-8")

    # 向导首步有可访问的明确选择；无 setup 时仍默认 GPU + 单次评审。
    assert 'id="quest-compute-options"' in txt and 'name="quest-compute-profile"' in txt
    assert 'id="quest-review-options"' in txt and 'name="quest-review-intensity"' in txt
    assert '<strong>本机 GPU / Conda / 联网</strong>' in txt
    assert '<strong>本机 CPU / Conda / 联网</strong>' in txt
    assert '<strong>每个评审点 1 次</strong>' in txt
    assert '<strong>关闭</strong>' in txt
    assert 'name="quest-compute-profile" value="local-gpu" checked' in txt
    assert 'name="quest-review-intensity" value="once" checked' in txt
    assert 'id="quest-gpu-device-panel"' in txt and 'name="quest-gpu-device"' in txt
    assert 'id="quest-gpu-device-count" aria-live="polite"' in txt
    assert 'id="quest-runtime-profile-confirmation" role="status" aria-live="polite"' in txt

    spec = txt[txt.index("function _questSpec("):txt.index("function _parseLocalSourceText(")]
    assert 'runtime_profile:_runtimeProfileFromControls("quest")' in spec
    assert 'const profile={version:options.version,compute_profile_id:raw.compute_profile_id,review_intensity:raw.review_intensity};' in txt
    assert 'profile.gpu_device_indices=raw.compute_profile_id==="local-gpu"?raw.gpu_device_indices:[]' in txt
    assert "runtime_profile_options" in txt and "compute_profiles" in txt
    assert "gpu_devices" in txt and "gpu_selection" in txt
    assert "review_intensities" in txt and "default_profile" in txt
    assert 'default_profile:{version:2,compute_profile_id:"local-gpu",review_intensity:"once",gpu_device_indices:[0]}' in txt
    assert 'source.version===1||source.version===2||source.version===3' in txt
    assert 'base.mode==="exact"' in txt
    for field in ("min_count", "max_count", "default_count"):
        assert f"base.{field}" in txt
    assert 'if(value.version===1) return !("gpu_device_indices" in value)' in txt
    assert 'else if(candidate.version===2)' in txt
    assert 'legacyDefaultCount=Math.min(FALLBACK_RUNTIME_PROFILE_OPTIONS.gpu_selection.requested_count,opts.gpu_selection.default_count)' in txt
    assert 'indices.slice(0,legacyDefaultCount)' in txt
    assert 'if(value.version===2&&options.version===3) return indices.length>0' in txt
    assert '_formatPreflight(_questWizard.preflight,_questWizard.localSources,_questWizard.spec.runtime_profile)' in txt

    # qualification 允许 v3 默认 exact 配置，但提交给密封边界时仍收敛到旧 v1；任何非默认池继续拒绝。
    qualification = txt[txt.index("function _qualificationRuntimeProfile("):txt.index("function _runtimeProfileRecord(")]
    assert '(runtime.version===2||runtime.version===3)' in qualification
    assert 'runtime.gpu_device_indices.length===defaultPool.length' in qualification
    assert 'if(!compatible) throw new Error(' in qualification
    assert 'return {version:1,compute_profile_id:"local-gpu",review_intensity:"once"}' in qualification
    assert 'spec.runtime_profile=_qualificationRuntimeProfile(spec.runtime_profile,_runtimeProfileOptions(_questSetup))' in spec

    # 已有任务从顶栏打开独立对话框；GET 使用 quest_id，POST body 只有后端冻结的两个字段。
    assert 'id="quest-runtime-profile-button"' in txt and '>运行配置</button>' in txt
    assert 'id="quest-runtime-profile-dialog"' in txt
    assert "当前任务后续研究轮使用" in txt
    assert 'aria-labelledby="quest-runtime-profile-title"' in txt
    assert 'oncancel="if(_runtimeProfileEditor.busy) event.preventDefault()"' in txt
    load = txt[txt.index("async function openQuestRuntimeProfile("):txt.index("async function saveQuestRuntimeProfile(")]
    assert '"/api/quest-runtime-profile?quest_id="+encodeURIComponent(questId)' in load
    assert "record.profile" in load and "_runtimeProfileIsAllowed(record.profile,options)" in load
    save = txt[txt.index("async function saveQuestRuntimeProfile("):txt.index("function _showQuestRuntime(")]
    assert 'const payload={quest_id:questId,runtime_profile:profile};' in save
    assert '_reliableJsonPost("/api/quest-runtime-profile",payload)' in save
    assert "_savedPendingRestartResponse(result.response,result.body,result.key)" in save
    assert "运行配置已保存，但自动重启调度待恢复" in save
    assert "再次保存将复用同一请求" in save
    assert "idempotency_key:" not in save
    assert 'name="runtime-compute-profile"' in txt
    assert 'name="runtime-review-intensity"' in txt
    assert 'id="runtime-gpu-device-panel"' in txt and 'name="runtime-gpu-device"' in txt

    # 不能暗示 stage 内热替换；GPU/Conda/review 同一耐久轮次整体应用。
    assert "运行中保存后，将等待当前研究轮/实验安全结束，再自动重启并应用" in txt
    assert "计算链路、Conda 环境与评审强度按同一耐久研究轮配置一起生效" in txt
    assert "不会立即热替换" in txt
    assert "当前安全阶段结束后自动应用" not in txt


def test_runtime_profile_card_layout_and_gpu_candidate_ui_contract():
    """Radio/checkbox 不得继承全宽输入框；v2 候选池与 v3 exact GPU 共用稳健卡片。"""
    txt = PAGE.read_text(encoding="utf-8")

    # 根因回归：全宽表单规则只命中文本型控件，不能再次吞掉 radio/checkbox。
    assert '.formfield input:not([type="radio"]):not([type="checkbox"]),.formfield select,.formfield textarea{' in txt
    assert '.formfield input,.formfield select,.formfield textarea{' not in txt
    assert '.runtime-option input[type="radio"]{box-sizing:border-box;width:17px;height:17px;' in txt
    assert 'padding:0;flex:0 0 17px;accent-color:var(--accent)' in txt
    assert '.runtime-option span{display:flex;flex:1 1 auto;flex-direction:column;min-width:0;overflow-wrap:anywhere}' in txt

    # GPU panel/chip 在新建和运行中编辑两处复用；候选 checkbox 也有固定尺寸。
    for scope in ("quest", "runtime"):
        assert f'id="{scope}-gpu-device-panel"' in txt
        assert f'id="{scope}-gpu-device-options"' in txt
        assert f'id="{scope}-gpu-device-count" aria-live="polite"' in txt
        assert f'id="{scope}-gpu-device-title"' in txt
        assert f'id="{scope}-gpu-device-help"' in txt
        assert f'name="{scope}-gpu-device"' in txt
    assert '.gpu-device-panel[hidden]{display:none}' in txt
    assert '.gpu-device-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(92px,1fr));gap:7px}' in txt
    assert '.gpu-device-chip input[type="checkbox"]{box-sizing:border-box;width:16px;height:16px;' in txt
    assert 'padding:0;flex:0 0 16px;accent-color:var(--accent)' in txt
    assert '.gpu-device-chip span{min-width:0;overflow-wrap:anywhere}' in txt
    assert '.gpu-device-chip:has(input:checked)' in txt

    # 平板两列、窄手机单列；不是仅靠默认 auto-fit 偶然换行。
    assert '@media(max-width:700px)' in txt
    assert '.gpu-device-options{grid-template-columns:repeat(2,minmax(0,1fr))}' in txt
    assert '@media(max-width:390px){.gpu-device-options{grid-template-columns:minmax(0,1fr)}}' in txt

    # 动态 setup 重绘必须同步生成 chip；设备标签只走 textContent，不能成为 HTML。
    assert 'function _renderRuntimeGpuChoices(' in txt
    assert 'label.className="gpu-device-chip"' in txt
    assert 'text.textContent=row.label' in txt
    assert 'text.innerHTML=row.label' not in txt
    assert 'options.version!==2&&options.version!==3' in txt
    assert 'const visible=(opts.version===2||opts.version===3)&&compute&&compute.value==="local-gpu"' in txt

    # v2 保持候选池 + 服务端固定绑定数；v3 明示自动检测及 exact 绑定张数。
    assert '"候选 "+selected+" 张 · 启动绑定 "+requested+" 张"' in txt
    assert 'title.textContent=opts.version===3?"本机自动检测到的 GPU":"选择候选 GPU"' in txt
    assert '"已选 "+selected+" 张 · 将绑定 "+selected+" 张"' in txt
    assert '"请至少选择 "+options.gpu_selection.min_count+" 张 GPU"' in txt
    assert '"最多选择 "+options.gpu_selection.max_count+" 张 GPU"' in txt
    assert 'summary+=" · GPU："+value.gpu_device_indices.join("、")+"（绑定"+value.gpu_device_indices.length+"张）"' in txt


def test_training_monitor_window_contract():
    """Bundle 的真实子进程日志、target 与可声明进度在独立窗口原位刷新。"""
    txt = PAGE.read_text(encoding="utf-8")
    assert 'id="training-monitor-button"' in txt and '>实验监控</button>' in txt
    assert 'id="training-monitor-dialog"' in txt
    assert 'id="training-monitor-targets"' in txt
    assert 'id="training-monitor-overall"' in txt
    assert 'id="training-monitor-detail"' in txt
    assert 'id="training-monitor-log-tabs"' in txt
    assert 'id="training-monitor-log"' in txt
    assert 'id="training-monitor-follow" checked' in txt
    assert "显示子进程真实 stdout / stderr 日志尾部" in txt
    assert "若实验没有输出总步数" in txt

    assert "const hasTrainingLive=" in txt
    assert "D.training_live=hasTrainingLive?" in txt
    assert "_legacyTrainingLive" in txt
    assert "monitor_api_available:false" in txt
    assert "当前 Web 服务进程启动较早" in txt
    assert "Bundle Codex 实时活动" in txt
    assert "live.agent_live_text" in txt
    assert "function renderTrainingMonitor(" in txt
    assert "function trainingMonitorLogScrolled(" in txt
    assert "renderTrainingMonitor(false); syncNarratorSessionStatus()" in txt
    assert 'raw.textContent=text' in txt
    assert 'tab.textContent="t"+log.target_id' in txt
    assert "raw.innerHTML" not in txt                         # 原始日志不得成为同源 HTML
    assert "tab.innerHTML" not in txt
    assert 'remaining>48){_trainingMonitorState.follow=false' in txt
    assert '.training-raw-log{' in txt and 'white-space:pre-wrap' in txt


def test_adaptpayload_contract_keys(tmp_path):
    """契约：assemble_db 产的顶层键覆盖 adaptPayload 消费的（tables + 派生对象），防 server/前端漂移。"""
    p = _payload(tmp_path)
    assert {"tables", "status_card", "live", "training_live", "notification", "ledger_by_cycle", "policy", "fs"} <= set(p)
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
def test_poll_refresh_preserves_scroll_and_explicit_switch_resets(tmp_path):
    """3 秒 DB 重绘保留主视区/嵌套输出位置；用户显式换页仍从页首开始。"""
    pf = tmp_path / "payload.json"
    pf.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")
    r = subprocess.run(
        ["node", str(SMOKE), str(PAGE), str(pf), "", "", "scroll"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "SCROLL_SMOKE_OK" in r.stdout, (
        f"stdout={r.stdout} stderr={r.stderr[:400]}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_question_detail_uses_actual_phases_and_switches_codex_output(tmp_path):
    payload = _payload(tmp_path)
    question_id = payload["tables"]["question"][0]["id"]
    switch_question_id = max(row["id"] for row in payload["tables"]["question"]) + 1
    payload["tables"]["question"].append({
        **payload["tables"]["question"][0], "id": switch_question_id,
        "parent_id": question_id, "text": "drawer-switch-target", "status": "open",
    })
    cycle = payload["tables"]["cycle"][0]
    cycle.update({"active_question_id": question_id, "route": "attack", "status": "bundle"})
    cycle_id = cycle["id"]
    payload["tables"]["runner_call"] = [
        {"id": 101, "cycle_id": cycle_id, "phase": "plan", "purpose": "plan-a1",
         "status": "success", "started_at": "t1", "finished_at": "t2"},
        {"id": 102, "cycle_id": cycle_id, "phase": "bundle", "purpose": "bundle-a1",
         "status": "running", "started_at": "t3", "finished_at": None},
    ]
    payload["tables"]["phase_commit"] = [
        {"id": 1, "cycle_id": cycle_id, "stage": "plan", "target_id": None}]
    payload["runner_output"] = [
        {"key": "plan", "at": "t2", "runner_call_id": 101, "cycle_id": cycle_id,
         "phase": "plan", "purpose": "plan-a1", "call_status": "success",
         "kind": "output", "text": "PLAN-ONLY-OUTPUT"},
        {"key": "bundle-live", "at": "t3", "runner_call_id": 102, "cycle_id": cycle_id,
         "phase": "bundle", "purpose": "bundle-a1", "call_status": "running",
         "kind": "live", "text": "LIVE-BUNDLE-OUTPUT"},
    ]
    payload["live"].update({
        "mode": "running", "inflight_cycle": f"c{cycle_id}",
        "orchestrator_active": True, "orchestrator_state": "running",
        "runner_call": payload["tables"]["runner_call"][1],
    })
    payload["status_card"]["active_question"] = {"id": f"q{question_id}", "text": "q"}
    pf = tmp_path / "question-payload.json"
    sf = tmp_path / "question-spec.json"
    df = tmp_path / "drawer-spec.json"
    pf.write_text(json.dumps(payload), encoding="utf-8")
    sf.write_text(json.dumps({
        "question_id": question_id,
        "present": ["实验规划", "实验构建与运行", "LIVE-BUNDLE-OUTPUT"],
        "absent": ["构想生成", "结果收口", "PLAN-ONLY-OUTPUT"],
        "select": {"cycle_id": cycle_id, "phase": "plan",
                   "present": ["PLAN-ONLY-OUTPUT"], "absent": ["LIVE-BUNDLE-OUTPUT"]},
    }), encoding="utf-8")
    df.write_text(json.dumps({
        "question_id": question_id, "runner_call_id": 102,
        "next_output": "LIVE-BUNDLE-OUTPUT-NEXT", "switch_question_id": switch_question_id,
    }), encoding="utf-8")
    r = subprocess.run(
        ["node", str(SMOKE), str(PAGE), str(pf), "", str(sf), "", str(df)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "SMOKE_OK" in r.stdout and "DRAWER_SMOKE_OK" in r.stdout, (
        f"stdout={r.stdout} stderr={r.stderr[:400]}")


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

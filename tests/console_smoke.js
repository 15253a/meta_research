// 步⑨ CP9.2 · 控制台前端 node 冒烟（test_console_frontend.py 调用）。
// 加载改后 views/console/index.html（提供最小 DOM stub、故意不定义 window → HEADLESS=true），证：
//   ① 页面脚本在真数据形状上加载 + render() 不抛 JS 错（渲染码保真）；
//   ② adaptPayload 把 console_server /api/db payload 映射回原型 DB 形状（真表平铺顶层 + 派生列）。
// 用法：node console_smoke.js <index.html 路径> <payload.json 路径>；SMOKE_OK → exit 0。
const fs = require('fs');
const [pagePath, payloadPath, forbiddenHtml, questionSpecPath, scrollSpec, drawerSpecPath] = process.argv.slice(2);
const html = fs.readFileSync(pagePath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
const htmlWrites = [];

// 通用假元素：Proxy 吸收任意属性/方法访问，让 render 调 innerHTML/classList/addEventListener/... 不抛。
function fakeEl(id) {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
                  id: id || '', scrollHeight: 0, scrollTop: 0, scrollLeft: 0,
                  clientHeight: 0, offsetWidth: 0, offsetHeight: 0 };
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'classList') return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (['addEventListener','removeEventListener','setAttribute','append','appendChild','remove','focus','scrollTo','getBoundingClientRect'].includes(k))
        return () => ({ left: 0, top: 0, width: 0, height: 0 });
      if (k === 'querySelector' || k === 'closest') return () => fakeEl();
      if (k === 'querySelectorAll' || k === 'children') return [];
      return () => fakeEl();
    },
    set(t, k, v) { if (k === 'innerHTML') htmlWrites.push(String(v)); t[k] = v; return true; },
  });
}
const fakeElements = new Map();
function fakeElementById(id) {
  if (!fakeElements.has(id)) fakeElements.set(id, fakeEl(id));
  return fakeElements.get(id);
}
let keyedScrollElements = [];
let runtimeControlState = null;
function fakeQuerySelector(selector) {
  if (runtimeControlState) {
    const prefix = 'input[name="' + runtimeControlState.scope + '-';
    if (selector === prefix + 'compute-profile"]:checked') return {value: runtimeControlState.compute};
    if (selector === prefix + 'review-intensity"]:checked') return {value: runtimeControlState.review};
  }
  return fakeEl();
}
function fakeQuerySelectorAll(selector) {
  if (selector === '[data-scroll-key]') return keyedScrollElements;
  if (selector === '[data-live-output="1"]') return keyedScrollElements.filter(el => el.dataset.liveOutput === '1');
  if (runtimeControlState
      && selector === 'input[name="' + runtimeControlState.scope + '-gpu-device"]:checked') {
    return runtimeControlState.gpu.map(index => ({value: String(index)}));
  }
  return [];
}
global.document = { getElementById: fakeElementById, querySelector: fakeQuerySelector, querySelectorAll: fakeQuerySelectorAll,
  createElement: () => fakeEl(), addEventListener: () => {}, body: fakeEl(), documentElement: fakeEl() };
// window 故意不定义 → HEADLESS=true（整合块 fetch/timer 跳过，只验 render 数据路径 + adaptPayload 纯函数）。

const ctx = {};
try {
  // __setDB/__applyLive/__setTab 是脚本作用域内的闭包 → 能重赋值 `let DB`/触发 live 覆盖/切标签（外部拿不到词法变量），供冒烟走真渲染路径。
  new Function(scripts + '\n; this.__adapt=adaptPayload; this.__render=render; this.__setDB=function(d){DB=d;};'
    + ' this.__applyLive=applyLive; this.__streamSync=streamSyncReal; this.__clamp=clampSelections; this.__fsRefresh=fsRefresh;'
    + ' this.__buildLive=buildLive; this.__setTab=function(k){state.tab=k;}; this.__tabs=NAV.map(x=>x[0]);'
    + ' this.__questionHTML=questionExecutionHTML; this.__setQuestionPhase=function(c,p){state.qExec={cycleId:c,phase:p};};'
    + ' this.__runnerStreamEvents=runnerStreamEvents;'
    + ' this.__runtimeOptions=_runtimeProfileOptions; this.__normalizeRuntimeProfile=_normalizeRuntimeProfile;'
    + ' this.__runtimeProfileIsAllowed=_runtimeProfileIsAllowed; this.__runtimeProfileFromControls=_runtimeProfileFromControls;'
    + ' this.__runtimeProfileSummary=_runtimeProfileSummary; this.__qualificationRuntimeProfile=_qualificationRuntimeProfile;'
    + ' this.__setQuestSetup=function(value){_questSetup=value;}; this.__renderPolling=function(){render({preserveScroll:true});};'
    + ' this.__narratorProgressState=_narratorProgressState;'
    + ' this.__go=go; this.__captureRenderScroll=_captureRenderScroll; this.__restoreRenderScroll=_restoreRenderScroll;'
    + ' this.__questionExecution=questionExecutionHTML; this.__questionCard=questionCardHTML;'
    + ' this.__setDrawerQuestion=function(qid){state.tab="tree";state.canvas.drawerQ=qid;state.selQ=qid;state.qExec={cycleId:null,phase:null};};'
    + ' this.__refreshDrawer=refreshQuestionDrawer; this.__selectDrawer=canvasSelect;').call(ctx);
} catch (e) { console.log('SMOKE_FAIL load/render: ' + e.message); process.exit(1); }

const narratorMessage = {id: 41};
const narratorSession = {state: 'establishing'};
const narratorPreparing = ctx.__narratorProgressState(narratorMessage, null, null, narratorSession);
const narratorQueued = ctx.__narratorProgressState(
  narratorMessage, {status: 'created', failure_kind: 'query_queued'}, null, narratorSession);
const narratorRunning = ctx.__narratorProgressState(
  narratorMessage, {status: 'running', failure_kind: null}, null, narratorSession);
const narratorFailed = ctx.__narratorProgressState(
  narratorMessage, {status: 'failed', failure_kind: 'provider_unavailable'}, null, narratorSession);
const narratorFinished = ctx.__narratorProgressState(
  narratorMessage, {status: 'success', failure_kind: null}, {id: 7}, narratorSession);
if (!narratorPreparing.active || !narratorPreparing.label.includes('准备上下文')
    || !narratorQueued.active || !narratorQueued.label.includes('已排队')
    || !narratorRunning.active || !narratorRunning.label.includes('建立 Codex session')
    || !narratorFailed.terminal || !narratorFailed.bad
    || narratorFinished.visible) {
  console.log('SMOKE_FAIL narrator progress state projection'); process.exit(1);
}

const fallbackRuntime = ctx.__runtimeOptions(null);
const fallbackProfile = ctx.__normalizeRuntimeProfile(null, fallbackRuntime);
if (fallbackRuntime.version !== 2 || fallbackProfile.version !== 2
    || fallbackProfile.compute_profile_id !== 'local-gpu'
    || fallbackProfile.review_intensity !== 'once'
    || fallbackRuntime.gpu_selection.requested_count !== 1
    || JSON.stringify(fallbackRuntime.gpu_devices) !== JSON.stringify([{index: 0, label: 'GPU 0'}])
    || JSON.stringify(fallbackProfile.gpu_device_indices) !== JSON.stringify([0])
    || fallbackRuntime.compute_profiles.find(row => row.id === 'local-gpu').label !== '本机 GPU / Conda / 联网'
    || fallbackRuntime.review_intensities.find(row => row.id === 'once').label !== '每个评审点 1 次') {
  console.log('SMOKE_FAIL runtime profile fallback'); process.exit(1);
}

const serverV2Source = {
  version: 2,
  compute_profiles: [
    {compute_profile_id: 'local-cpu', label: '服务端 CPU'},
    {compute_profile_id: 'local-gpu', label: '服务端 GPU'},
  ],
  gpu_devices: [
    {index: 7, label: 'GPU Seven'},
    {index: 2, label: 'GPU Two'},
    {index: 7, label: 'duplicate ignored'},
    {index: -1, label: 'invalid ignored'},
  ],
  gpu_selection: {requested_count: 2},
  review_intensities: [
    {review_intensity: 'off', label: '服务端关闭'},
    {review_intensity: 'once', label: '服务端一次'},
  ],
  default_profile: {
    version: 2, compute_profile_id: 'local-gpu', review_intensity: 'once',
    gpu_device_indices: [7, 2, 7, 99],
  },
};
const serverV2 = ctx.__runtimeOptions({runtime_profile_options: serverV2Source});
const normalizedV2 = ctx.__normalizeRuntimeProfile({
  profile: {version: 2, compute_profile_id: 'local-gpu', review_intensity: 'off',
            gpu_device_indices: [7, 99, 2, 7]},
}, serverV2);
const insufficientV2 = ctx.__normalizeRuntimeProfile({
  version: 2, compute_profile_id: 'local-gpu', review_intensity: 'once', gpu_device_indices: [7],
}, serverV2);
const cpuV2 = ctx.__normalizeRuntimeProfile({
  version: 2, compute_profile_id: 'local-cpu', review_intensity: 'off', gpu_device_indices: [2, 7],
}, serverV2);
if (serverV2.version !== 2 || serverV2.default_profile.version !== 2
    || serverV2.compute_profiles.length !== 2
    || serverV2.compute_profiles[1].label !== '服务端 GPU'
    || serverV2.review_intensities[0].label !== '服务端关闭'
    || JSON.stringify(serverV2.gpu_devices.map(row => row.index)) !== JSON.stringify([2, 7])
    || serverV2.gpu_selection.requested_count !== 2
    || JSON.stringify(serverV2.default_profile.gpu_device_indices) !== JSON.stringify([2, 7])
    || JSON.stringify(normalizedV2.gpu_device_indices) !== JSON.stringify([2, 7])
    || JSON.stringify(insufficientV2.gpu_device_indices) !== JSON.stringify([2, 7])
    || cpuV2.version !== 2 || cpuV2.gpu_device_indices.length !== 0
    || !ctx.__runtimeProfileIsAllowed(normalizedV2, serverV2)
    || ctx.__runtimeProfileIsAllowed({
      version: 2, compute_profile_id: 'local-gpu', review_intensity: 'once', gpu_device_indices: [7],
    }, serverV2)
    || ctx.__runtimeProfileIsAllowed({
      version: 2, compute_profile_id: 'local-gpu', review_intensity: 'once', gpu_device_indices: [7, 2],
    }, serverV2)) {
  console.log('SMOKE_FAIL runtime profile v2 server normalization'); process.exit(1);
}

ctx.__setQuestSetup({runtime_profile_options: serverV2Source});
runtimeControlState = {scope: 'quest', compute: 'local-gpu', review: 'off', gpu: [7, 2]};
const selectedV2 = ctx.__runtimeProfileFromControls('quest');
const summaryV2 = ctx.__runtimeProfileSummary(selectedV2);
runtimeControlState = {scope: 'quest', compute: 'local-cpu', review: 'once', gpu: [2, 7]};
const selectedCpuV2 = ctx.__runtimeProfileFromControls('quest');
let insufficientSelectionRejected = false;
runtimeControlState = {scope: 'quest', compute: 'local-gpu', review: 'once', gpu: [7]};
try { ctx.__runtimeProfileFromControls('quest'); } catch (error) {
  insufficientSelectionRejected = /至少选择 2 张候选 GPU/.test(String(error && error.message));
}
if (selectedV2.version !== 2 || selectedV2.review_intensity !== 'off'
    || JSON.stringify(selectedV2.gpu_device_indices) !== JSON.stringify([2, 7])
    || !summaryV2.includes('GPU 候选：2、7（启动绑定 2 张）')
    || selectedCpuV2.version !== 2 || selectedCpuV2.gpu_device_indices.length !== 0
    || !insufficientSelectionRejected) {
  console.log('SMOKE_FAIL runtime profile v2 control selection'); process.exit(1);
}

const serverV3Source = {
  version: 3,
  compute_profiles: [
    {compute_profile_id: 'local-gpu', label: '本机自动检测 GPU'},
    {compute_profile_id: 'local-cpu', label: '本机 CPU'},
  ],
  gpu_devices: [
    {index: 5, label: 'RTX Five'},
    {index: 0, label: 'RTX Zero'},
    {index: 2, label: 'RTX Two'},
    {index: 7, label: 'RTX Seven'},
  ],
  gpu_selection: {mode: 'exact', min_count: 1, max_count: 4, default_count: 4},
  review_intensities: [
    {review_intensity: 'once', label: '一次'},
    {review_intensity: 'off', label: '关闭'},
  ],
  // 新任务遵循服务端 v3 默认：尽可能用满当前允许的本机 GPU。
  default_profile: {
    version: 3, compute_profile_id: 'local-gpu', review_intensity: 'once',
    gpu_device_indices: [7, 5, 2, 0],
  },
};
const serverV3 = ctx.__runtimeOptions({runtime_profile_options: serverV3Source});
const exactOneV3 = ctx.__normalizeRuntimeProfile({
  version: 3, compute_profile_id: 'local-gpu', review_intensity: 'off', gpu_device_indices: [5],
}, serverV3);
const exactTwoV3 = ctx.__normalizeRuntimeProfile({
  version: 3, compute_profile_id: 'local-gpu', review_intensity: 'once', gpu_device_indices: [2, 0],
}, serverV3);
const v1MigratedToV3 = ctx.__normalizeRuntimeProfile({
  version: 1, compute_profile_id: 'local-gpu', review_intensity: 'once',
}, serverV3);
const legacyV2 = {
  version: 2, compute_profile_id: 'local-gpu', review_intensity: 'once',
  gpu_device_indices: [0, 2, 5, 7],
};
const v2MigratedToV3 = ctx.__normalizeRuntimeProfile(legacyV2, serverV3);
const staleLegacyV2 = {
  version: 2, compute_profile_id: 'local-gpu', review_intensity: 'once',
  gpu_device_indices: [0, 2, 99],
};
const staleV2MigratedToV3 = ctx.__normalizeRuntimeProfile(staleLegacyV2, serverV3);
const invalidV3Catalog = ctx.__runtimeOptions({runtime_profile_options: {
  ...serverV3Source, gpu_selection: {mode: 'candidate_pool', min_count: 1, max_count: 4, default_count: 4},
}});
const qualificationV3 = ctx.__qualificationRuntimeProfile(serverV3.default_profile, serverV3);
let nonDefaultQualificationRejected = false;
try { ctx.__qualificationRuntimeProfile(exactTwoV3, serverV3); } catch (error) {
  nonDefaultQualificationRejected = /恢复默认运行配置/.test(String(error && error.message));
}
if (serverV3.version !== 3 || serverV3.default_profile.version !== 3
    || serverV3.gpu_selection.mode !== 'exact'
    || serverV3.gpu_selection.min_count !== 1 || serverV3.gpu_selection.max_count !== 4
    || serverV3.gpu_selection.default_count !== 4
    || JSON.stringify(serverV3.gpu_devices.map(row => row.index)) !== JSON.stringify([0, 2, 5, 7])
    || JSON.stringify(serverV3.default_profile.gpu_device_indices) !== JSON.stringify([0, 2, 5, 7])
    || JSON.stringify(exactOneV3.gpu_device_indices) !== JSON.stringify([5])
    || JSON.stringify(exactTwoV3.gpu_device_indices) !== JSON.stringify([0, 2])
    || JSON.stringify(v1MigratedToV3.gpu_device_indices) !== JSON.stringify([0, 2, 5, 7])
    || JSON.stringify(v2MigratedToV3.gpu_device_indices) !== JSON.stringify([0])
    || !ctx.__runtimeProfileIsAllowed(legacyV2, serverV3)
    || !ctx.__runtimeProfileIsAllowed(staleLegacyV2, serverV3)
    || JSON.stringify(staleV2MigratedToV3.gpu_device_indices) !== JSON.stringify([0])
    || !ctx.__runtimeProfileIsAllowed(exactTwoV3, serverV3)
    || ctx.__runtimeProfileIsAllowed({
      version: 3, compute_profile_id: 'local-gpu', review_intensity: 'once', gpu_device_indices: [],
    }, serverV3)
    || invalidV3Catalog.version !== 2
    || JSON.stringify(qualificationV3) !== JSON.stringify({
      version: 1, compute_profile_id: 'local-gpu', review_intensity: 'once',
    })
    || !nonDefaultQualificationRejected) {
  console.log('SMOKE_FAIL runtime profile v3 exact normalization/migration'); process.exit(1);
}

ctx.__setQuestSetup({runtime_profile_options: serverV3Source});
runtimeControlState = {scope: 'quest', compute: 'local-gpu', review: 'once', gpu: [2, 0]};
const selectedV3 = ctx.__runtimeProfileFromControls('quest');
const summaryV3 = ctx.__runtimeProfileSummary(selectedV3);
runtimeControlState = {scope: 'quest', compute: 'local-gpu', review: 'off', gpu: [5]};
const selectedOneV3 = ctx.__runtimeProfileFromControls('quest');
runtimeControlState = {scope: 'quest', compute: 'local-cpu', review: 'once', gpu: [0, 2, 5, 7]};
const selectedCpuV3 = ctx.__runtimeProfileFromControls('quest');
let minimumV3Rejected = false;
runtimeControlState = {scope: 'quest', compute: 'local-gpu', review: 'once', gpu: []};
try { ctx.__runtimeProfileFromControls('quest'); } catch (error) {
  minimumV3Rejected = /至少选择 1 张 GPU/.test(String(error && error.message));
}
let maximumV3Rejected = false;
runtimeControlState = {scope: 'quest', compute: 'local-gpu', review: 'once', gpu: [0, 2, 5, 7, 99]};
try { ctx.__runtimeProfileFromControls('quest'); } catch (error) {
  maximumV3Rejected = /最多选择 4 张 GPU/.test(String(error && error.message));
}
if (selectedV3.version !== 3
    || JSON.stringify(selectedV3.gpu_device_indices) !== JSON.stringify([0, 2])
    || summaryV3 !== '计算链路：本机自动检测 GPU · 评审：一次 · GPU：0、2（绑定2张）'
    || JSON.stringify(selectedOneV3.gpu_device_indices) !== JSON.stringify([5])
    || selectedCpuV3.version !== 3 || selectedCpuV3.gpu_device_indices.length !== 0
    || !minimumV3Rejected || !maximumV3Rejected) {
  console.log('SMOKE_FAIL runtime profile v3 exact control selection'); process.exit(1);
}

const serverV1Source = {
  version: 1,
  compute_profiles: [{compute_profile_id: 'local-cpu', label: '服务端 CPU'}],
  review_intensities: [{review_intensity: 'off', label: '服务端关闭'}],
  default_profile: {version: 1, compute_profile_id: 'local-cpu', review_intensity: 'off'},
};
const serverV1 = ctx.__runtimeOptions({runtime_profile_options: serverV1Source});
const normalizedV1 = ctx.__normalizeRuntimeProfile({
  version: 2, compute_profile_id: 'local-cpu', review_intensity: 'off', gpu_device_indices: [0],
}, serverV1);
ctx.__setQuestSetup({runtime_profile_options: serverV1Source});
runtimeControlState = {scope: 'quest', compute: 'local-cpu', review: 'off', gpu: [2, 7]};
const selectedV1 = ctx.__runtimeProfileFromControls('quest');
runtimeControlState = null;
ctx.__setQuestSetup(null);
if (serverV1.version !== 1 || serverV1.default_profile.version !== 1
    || serverV1.compute_profiles.length !== 1 || serverV1.compute_profiles[0].label !== '服务端 CPU'
    || serverV1.review_intensities[0].label !== '服务端关闭'
    || serverV1.default_profile.compute_profile_id !== 'local-cpu'
    || serverV1.gpu_devices.length !== 0 || Object.keys(serverV1.gpu_selection).length !== 0
    || 'gpu_device_indices' in serverV1.default_profile
    || normalizedV1.version !== 1 || 'gpu_device_indices' in normalizedV1
    || selectedV1.version !== 1 || 'gpu_device_indices' in selectedV1
    || !ctx.__runtimeProfileIsAllowed(selectedV1, serverV1)) {
  console.log('SMOKE_FAIL runtime profile v1 server compatibility'); process.exit(1);
}

const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const D = ctx.__adapt(payload);
const shapeOk = Array.isArray(D.question) && Array.isArray(D.baseline) && Array.isArray(D.decision)
  && D.status_card && Array.isArray(D.notification) && D.policy && D.live !== undefined
  && D.training_live && typeof D.training_live === 'object'
  && D.budget && typeof D.budget.B_t === 'number'                               // budget 派生就位（顶栏每拍必读）
  && (D.decision.length === 0 || typeof D.decision[0].summary === 'string');
if (!shapeOk) { console.log('SMOKE_FAIL adapt shape'); process.exit(1); }
for (const mode of ['running', 'awaiting_user', 'paused', 'interrupted', 'idle']) {
  const active = mode !== 'interrupted' && mode !== 'idle';
  const live = ctx.__buildLive({live: {mode, orchestrator_active: active}});
  if (live.idle !== (mode === 'idle')) {
    console.log('SMOKE_FAIL live mode mislabeled idle: ' + mode); process.exit(1);
  }
  if (live.alive !== active || live.interrupted !== (mode === 'interrupted')) {
    console.log('SMOKE_FAIL live owner/interrupted semantics: ' + mode); process.exit(1);
  }
}

// 真数据模式装配：DB ← 真投影；applyLive 用真 payload.live 覆盖（LIVE_MODE=true）；streamSyncReal 铺真事件流。
ctx.__setDB(D);
try { ctx.__applyLive(payload); } catch (e) { console.log('SMOKE_FAIL applyLive: ' + e.message); process.exit(1); }
try { ctx.__clamp(); } catch (e) { console.log('SMOKE_FAIL clamp: ' + e.message); process.exit(1); }   // mock 默认选中夹到真 id（同 refreshDB）
try { ctx.__streamSync(); } catch (e) { console.log('SMOKE_FAIL streamSync: ' + e.message); process.exit(1); }
try { ctx.__fsRefresh(); } catch (e) { console.log('SMOKE_FAIL fsRefresh: ' + e.message); process.exit(1); }   // 真文件树（DB.fs）渲染路径
if (Array.isArray(D.runner_output) && D.runner_output.some(row => row && row.kind === 'live')) {
  const feed = ctx.__runnerStreamEvents();
  if (feed.some(row => D.runner_output.some(source => source && source.kind === 'live'
      && row.key === 'codex:' + String(source.key || '')))
      || !feed.some(row => String(row.key || '').includes(':legacy:') || row.activityKind)) {
    console.log('SMOKE_FAIL aggregate live row was not converted to short Codex activities'); process.exit(1);
  }
}

if (questionSpecPath) {
  const spec = JSON.parse(fs.readFileSync(questionSpecPath, 'utf8'));
  let detail = ctx.__questionHTML(spec.question_id);
  for (const marker of spec.present || []) {
    if (!detail.includes(marker)) { console.log('SMOKE_FAIL question detail missing: ' + marker); process.exit(1); }
  }
  for (const marker of spec.absent || []) {
    if (detail.includes(marker)) { console.log('SMOKE_FAIL question detail invented: ' + marker); process.exit(1); }
  }
  if (spec.select) {
    ctx.__setQuestionPhase(spec.select.cycle_id, spec.select.phase);
    detail = ctx.__questionHTML(spec.question_id);
    for (const marker of spec.select.present || []) {
      if (!detail.includes(marker)) { console.log('SMOKE_FAIL selected phase missing: ' + marker); process.exit(1); }
    }
    for (const marker of spec.select.absent || []) {
      if (detail.includes(marker)) { console.log('SMOKE_FAIL selected phase leaked: ' + marker); process.exit(1); }
    }
  }
}

if (drawerSpecPath) {
  const spec = JSON.parse(fs.readFileSync(drawerSpecPath, 'utf8'));
  const execution = fakeEl('drawer-execution');
  const card = fakeEl('drawer-card');
  const drawer = fakeEl('cvdrawer-live');
  drawer.dataset.questionId = String(spec.question_id);
  drawer.dataset.scrollKey = 'question-drawer-' + spec.question_id;
  drawer.innerHTML = 'STABLE-DRAWER-SHELL';
  drawer.scrollHeight = 1100; drawer.clientHeight = 300; drawer.scrollTop = 237; drawer.scrollLeft = 13;
  drawer.querySelector = selector => selector === '[data-drawer-region="execution"]' ? execution
    : selector === '[data-drawer-region="card"]' ? card : null;
  fakeElements.set('cvdrawer', drawer);
  ctx.__setDrawerQuestion(spec.question_id);
  execution.__drawerHTML = ctx.__questionExecution(spec.question_id);
  card.__drawerHTML = ctx.__questionCard(spec.question_id);
  card.innerHTML = 'STATIC-CARD-SENTINEL';
  keyedScrollElements = [drawer];

  const prior = D.runner_output.find(row => Number(row.runner_call_id) === Number(spec.runner_call_id)
    && row.kind === 'live');
  D.runner_output.push(Object.assign({}, prior || {}, {
    key: 'drawer-refresh-next', runner_call_id: spec.runner_call_id, kind: 'live', text: spec.next_output,
  }));
  const identity = fakeElementById('cvdrawer');
  ctx.__renderPolling();
  if (fakeElementById('cvdrawer') !== identity || drawer.innerHTML !== 'STABLE-DRAWER-SHELL') {
    console.log('SMOKE_FAIL polling replaced/flickered open question drawer'); process.exit(1);
  }
  if (!execution.innerHTML.includes(spec.next_output) || card.innerHTML !== 'STATIC-CARD-SENTINEL') {
    console.log('SMOKE_FAIL polling did not incrementally refresh drawer execution'); process.exit(1);
  }
  if (drawer.scrollTop !== 237 || drawer.scrollLeft !== 13) {
    console.log('SMOKE_FAIL polling reset open drawer scroll'); process.exit(1);
  }
  drawer.scrollTop = 321; drawer.scrollLeft = 17;
  fakeElementById('cvview').querySelectorAll = () => [];
  ctx.__selectDrawer(spec.switch_question_id);
  if (drawer.scrollTop !== 0 || drawer.scrollLeft !== 0
      || drawer.dataset.questionId !== String(spec.switch_question_id)
      || !drawer.innerHTML.includes('Q' + spec.switch_question_id)) {
    console.log('SMOKE_FAIL explicit question switch did not reset drawer'); process.exit(1);
  }
  console.log('DRAWER_SMOKE_OK');
}

// 逐个渲染全部标签页（render() 只渲当前 state.tab；不遍历则其余 8 页的真数据 null 崩溃不暴露）——收全部失败再报。
const fails = [];
for (const tab of ctx.__tabs) {
  ctx.__setTab(tab);
  try { ctx.__render(); } catch (e) { fails.push(tab + ': ' + e.message); }
}
if (fails.length) { console.log('SMOKE_FAIL render tabs:\n  ' + fails.join('\n  ')); process.exit(1); }
if (scrollSpec === 'scroll') {
  const stage = fakeElementById('stage-page');
  ctx.__setTab('cycles'); ctx.__render();
  stage.scrollHeight = 2400; stage.clientHeight = 600; stage.scrollTop = 487; stage.scrollLeft = 17;
  ctx.__renderPolling();
  if (stage.scrollTop !== 487 || stage.scrollLeft !== 17) {
    console.log('SMOKE_FAIL polling render reset stage scroll'); process.exit(1);
  }
  ctx.__go('evidence');
  if (stage.scrollTop !== 0 || stage.scrollLeft !== 0) {
    console.log('SMOKE_FAIL explicit tab switch retained stage scroll'); process.exit(1);
  }

  const oldOutput = fakeEl('old-output');
  oldOutput.dataset.scrollKey = 'runner-output-102';
  oldOutput.scrollHeight = 500; oldOutput.clientHeight = 100; oldOutput.scrollTop = 145; oldOutput.scrollLeft = 9;
  keyedScrollElements = [oldOutput];
  const middleSnapshot = ctx.__captureRenderScroll();
  const refreshedOutput = fakeEl('refreshed-output');
  refreshedOutput.dataset.scrollKey = 'runner-output-102';
  refreshedOutput.scrollHeight = 760; refreshedOutput.clientHeight = 100;
  keyedScrollElements = [refreshedOutput]; ctx.__restoreRenderScroll(middleSnapshot);
  if (refreshedOutput.scrollTop !== 145 || refreshedOutput.scrollLeft !== 9) {
    console.log('SMOKE_FAIL polling render reset nested output scroll'); process.exit(1);
  }

  oldOutput.scrollHeight = 500; oldOutput.clientHeight = 100; oldOutput.scrollTop = 400; oldOutput.scrollLeft = 0;
  keyedScrollElements = [oldOutput];
  const bottomSnapshot = ctx.__captureRenderScroll();
  refreshedOutput.scrollHeight = 760; refreshedOutput.clientHeight = 100; refreshedOutput.scrollTop = 0;
  keyedScrollElements = [refreshedOutput]; ctx.__restoreRenderScroll(bottomSnapshot);
  if (refreshedOutput.scrollTop !== 760) {
    console.log('SMOKE_FAIL bottom-follow output stopped following'); process.exit(1);
  }
  console.log('SCROLL_SMOKE_OK');
}
if (forbiddenHtml && htmlWrites.some(value => value.includes(forbiddenHtml))) {
  console.log('SMOKE_FAIL raw HTML reached innerHTML: ' + forbiddenHtml); process.exit(1);
}
console.log('SMOKE_OK tables=' + Object.keys(payload.tables).length + ' q=' + D.question.length
            + ' dec=' + D.decision.length + ' B_t=' + D.budget.B_t + ' tabs=' + ctx.__tabs.length);
process.exit(0);

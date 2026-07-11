// 步⑨ CP9.2 · 控制台前端 node 冒烟（test_console_frontend.py 调用）。
// 加载改后 views/console/index.html（提供最小 DOM stub、故意不定义 window → HEADLESS=true），证：
//   ① 页面脚本在真数据形状上加载 + render() 不抛 JS 错（渲染码保真）；
//   ② adaptPayload 把 console_server /api/db payload 映射回原型 DB 形状（真表平铺顶层 + 派生列）。
// 用法：node console_smoke.js <index.html 路径> <payload.json 路径>；SMOKE_OK → exit 0。
const fs = require('fs');
const [pagePath, payloadPath, forbiddenHtml] = process.argv.slice(2);
const html = fs.readFileSync(pagePath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
const htmlWrites = [];

// 通用假元素：Proxy 吸收任意属性/方法访问，让 render 调 innerHTML/classList/addEventListener/... 不抛。
function fakeEl() {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
                  scrollHeight: 0, scrollTop: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0 };
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
global.document = { getElementById: () => fakeEl(), querySelector: () => fakeEl(), querySelectorAll: () => [],
  createElement: () => fakeEl(), addEventListener: () => {}, body: fakeEl(), documentElement: fakeEl() };
// window 故意不定义 → HEADLESS=true（整合块 fetch/timer 跳过，只验 render 数据路径 + adaptPayload 纯函数）。

const ctx = {};
try {
  // __setDB/__applyLive/__setTab 是脚本作用域内的闭包 → 能重赋值 `let DB`/触发 live 覆盖/切标签（外部拿不到词法变量），供冒烟走真渲染路径。
  new Function(scripts + '\n; this.__adapt=adaptPayload; this.__render=render; this.__setDB=function(d){DB=d;};'
    + ' this.__applyLive=applyLive; this.__streamSync=streamSyncReal; this.__clamp=clampSelections; this.__fsRefresh=fsRefresh;'
    + ' this.__buildLive=buildLive; this.__setTab=function(k){state.tab=k;}; this.__tabs=NAV.map(x=>x[0]);').call(ctx);
} catch (e) { console.log('SMOKE_FAIL load/render: ' + e.message); process.exit(1); }

const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const D = ctx.__adapt(payload);
const shapeOk = Array.isArray(D.question) && Array.isArray(D.baseline) && Array.isArray(D.decision)
  && D.status_card && Array.isArray(D.notification) && D.policy && D.live !== undefined
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

// 逐个渲染全部标签页（render() 只渲当前 state.tab；不遍历则其余 8 页的真数据 null 崩溃不暴露）——收全部失败再报。
const fails = [];
for (const tab of ctx.__tabs) {
  ctx.__setTab(tab);
  try { ctx.__render(); } catch (e) { fails.push(tab + ': ' + e.message); }
}
if (fails.length) { console.log('SMOKE_FAIL render tabs:\n  ' + fails.join('\n  ')); process.exit(1); }
if (forbiddenHtml && htmlWrites.some(value => value.includes(forbiddenHtml))) {
  console.log('SMOKE_FAIL raw HTML reached innerHTML: ' + forbiddenHtml); process.exit(1);
}
console.log('SMOKE_OK tables=' + Object.keys(payload.tables).length + ' q=' + D.question.length
            + ' dec=' + D.decision.length + ' B_t=' + D.budget.B_t + ' tabs=' + ctx.__tabs.length);
process.exit(0);

// 浏览器控制面安全冒烟：capability、fail-closed 连接遮罩、POST 幂等键生命周期。
const fs = require('fs');
const page = fs.readFileSync(process.argv[2], 'utf8');
const begin = page.indexOf('const HEADLESS = (typeof window === "undefined")');
const end = page.indexOf('const state = {', begin);
if (begin < 0 || end < 0) { console.log('AUTH_SMOKE_FAIL missing auth block'); process.exit(1); }
const source = page.slice(begin, end);
const token = 'ab'.repeat(32);
const storage = new Map();
const replacements = [];
const notices = [];
const guard = { style: {}, textContent: '' };
global.window = {
  location: { hash: '#token=' + token, pathname: '/console/', search: '?view=live' },
  history: { replaceState(_state, _title, url) { replacements.push(url); window.location.hash = ''; } },
  sessionStorage: {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
  },
  crypto: require('crypto').webcrypto,
};
global.document = { title: 'console', getElementById() { return guard; }, createElement() { return guard; },
  body: { appendChild() {} } };
global.toast = m => notices.push(m);
global.emitEvent = e => notices.push(e.text);

const calls = [];
global.fetch = async (url, options) => { calls.push({url, options}); return {status: 200}; };
const ctx = {};
try {
  new Function(source + '\nthis.apiFetch=apiFetch; this.apiPost=apiPost; this.clearCapability=clearConsoleCapability;'
    + ' this.showGuard=showConnectionGuard; this.clearGuard=clearConnectionGuard;'
    + ' this.markFresh=markConsoleSnapshotFresh;'
    + ' this.expireSnapshot=()=>{_lastDbSuccessAt=Date.now()-SNAPSHOT_MAX_AGE_MS-1;};'
    + ' this.isReady=()=>_consoleReady;').call(ctx);
} catch (e) {
  console.log('AUTH_SMOKE_FAIL load: ' + e.message); process.exit(1);
}

function check(ok, message) { if (!ok) throw new Error(message); }
(async () => {
  check(replacements.length === 1 && replacements[0] === '/console/?view=live', 'fragment not cleared');
  check([...storage.values()][0] === token, 'token not stored in sessionStorage');
  check(!ctx.isReady(), 'capability alone incorrectly enabled live controls before a real DB snapshot');

  await ctx.apiFetch('/api/db');
  check(calls.length === 1, 'api request not issued');
  check(calls[0].options.headers.get('Authorization') === 'Bearer ' + token, 'Bearer missing');
  check(calls[0].options.credentials === 'omit' && calls[0].options.redirect === 'error', 'safe fetch flags missing');

  // 网络失败必须立即恢复全屏遮罩；只有后续真 /api/db 成功才会由 refreshDB 清除。
  ctx.markFresh();
  check(ctx.isReady() && guard.style.display === 'none', 'healthy DB state did not enable controls');

  const beforeStale = calls.length;
  ctx.expireSnapshot();
  let rejected = false;
  try { await ctx.apiPost('/api/message', {text: 'stale-must-not-send'}); } catch (_e) { rejected = true; }
  check(rejected && calls.length === beforeStale && !ctx.isReady(), 'stale snapshot did not refuse POST');
  check(guard.style.display === 'flex' && guard.textContent.includes('快照已过期'), 'stale snapshot guard missing');
  ctx.markFresh();

  global.fetch = async (url, options) => { calls.push({url, options}); throw new Error('offline'); };
  rejected = false;
  try { await ctx.apiFetch('/api/db'); } catch (_e) { rejected = true; }
  check(rejected && !ctx.isReady(), 'network failure did not disable controls');
  check(guard.style.display === 'flex' && guard.textContent.includes('网络错误'), 'network failure guard missing');

  ctx.clearGuard();
  global.fetch = async (url, options) => {
    calls.push({url, options});
    const key = options.headers.get('Idempotency-Key');
    const entry = [...storage.entries()].find(([k]) => k.includes('pending_posts'));
    check(entry && Object.values(JSON.parse(entry[1])).includes(key), 'idempotency key was not durable before fetch');
    return {status: 503, ok: false, async json() { return {error: 'late failure'}; }};
  };
  await ctx.apiPost('/api/message', {text: 'pause'});
  const firstPost = calls[calls.length - 1];
  const idem = firstPost.options.headers.get('Idempotency-Key');
  check(/^[0-9a-f]{32}$/.test(idem), 'client idempotency key missing');
  check([...storage.keys()].some(k => k.includes('pending_posts')), '5xx cleared pending key');
  check(!ctx.isReady() && guard.style.display === 'flex' && guard.textContent.includes('HTTP 503'),
    '5xx did not keep the fail-closed guard');
  ctx.markFresh();                         // 模拟下一次 refreshDB 真成功；失败遮罩不能由 POST 自行解除
  global.fetch = async (url, options) => {
    calls.push({url, options});
    const key = options.headers.get('Idempotency-Key');
    return {status: 200, ok: true, async json() { return {queued: {idempotency_key: 'console-' + key}}; }};
  };
  await ctx.apiPost('/api/message', {text: 'pause'});
  const secondPost = calls[calls.length - 1];
  check(secondPost.options.headers.get('Idempotency-Key') === idem, 'retry did not reuse key');
  const pendingEntry = [...storage.entries()].find(([k]) => k.includes('pending_posts'));
  check(pendingEntry && Object.keys(JSON.parse(pendingEntry[1])).length === 0, '2xx echo did not clear pending key');

  // 2xx 但缺失/伪造回显不能证明服务端接纳；4xx 是确定性拒绝，应清理键。
  global.fetch = async (url, options) => {
    calls.push({url, options});
    return {status: 200, ok: true, async json() { return {queued: {idempotency_key: 'console-mismatch'}}; }};
  };
  await ctx.apiPost('/api/message', {text: 'bad-echo'});
  let pendingMap = JSON.parse(storage.get(pendingEntry[0]));
  check(Object.keys(pendingMap).length === 1, 'mismatched 2xx echo cleared pending key');
  global.fetch = async (url, options) => {
    calls.push({url, options}); return {status: 400, ok: false, async json() { return {error: 'bad request'}; }};
  };
  await ctx.apiPost('/api/message', {text: 'bad-echo'});
  pendingMap = JSON.parse(storage.get(pendingEntry[0]));
  check(Object.keys(pendingMap).length === 0, '4xx did not clear pending key');

  // fetch 抛错时键已先持久化；后续同 operation 必须复用。
  global.fetch = async (url, options) => { calls.push({url, options}); throw new Error('connection reset'); };
  rejected = false;
  try { await ctx.apiPost('/api/message', {text: 'network-retry'}); } catch (_e) { rejected = true; }
  check(rejected, 'network POST unexpectedly succeeded');
  pendingMap = JSON.parse(storage.get(pendingEntry[0]));
  const networkKey = Object.values(pendingMap)[0];
  check(/^[0-9a-f]{32}$/.test(networkKey || ''), 'network failure lost persisted key');
  ctx.markFresh();
  global.fetch = async (url, options) => {
    calls.push({url, options});
    const key = options.headers.get('Idempotency-Key');
    return {status: 200, ok: true, async json() { return {queued: {idempotency_key: 'console-' + key}}; }};
  };
  await ctx.apiPost('/api/message', {text: 'network-retry'});
  check(calls[calls.length - 1].options.headers.get('Idempotency-Key') === networkKey,
    'network retry did not reuse persisted key');

  // 同标签页并发的同 operation 共用一键；两次 5xx 后仍保留该唯一键。
  storage.set(pendingEntry[0], '{}');
  const deferred = [];
  global.fetch = (url, options) => {
    calls.push({url, options});
    return new Promise(resolve => deferred.push({resolve, options}));
  };
  const concurrentA = ctx.apiPost('/api/directive', {action: 'confirm', directive_id: 7, reason: ''});
  const concurrentB = ctx.apiPost('/api/directive', {action: 'confirm', directive_id: 7, reason: ''});
  check(deferred.length === 2, 'concurrent operations did not reach fetch');
  const concurrentKey = deferred[0].options.headers.get('Idempotency-Key');
  check(deferred[1].options.headers.get('Idempotency-Key') === concurrentKey,
    'same operation did not share one in-tab idempotency key');
  deferred.forEach(d => d.resolve({status: 503, ok: false, async json() { return {error: 'retry'}; }}));
  await Promise.all([concurrentA, concurrentB]);
  pendingMap = JSON.parse(storage.get(pendingEntry[0]));
  check(Object.keys(pendingMap).length === 1 && Object.values(pendingMap)[0] === concurrentKey,
    'concurrent 5xx lost or forked pending key');
  ctx.markFresh();

  // 64 个未决 operation 是安全上限：清理格式非法项后 fail closed，不淘汰任何可重试键。
  const full = {'not-json': 'also-invalid'};
  for (let i = 1; i <= 64; i++) {
    full[JSON.stringify(['/api/message', {slot: i}])] = i.toString(16).padStart(32, '0');
  }
  storage.set(pendingEntry[0], JSON.stringify(full));
  const callsBeforeFull = calls.length;
  rejected = false;
  try { await ctx.apiPost('/api/message', {slot: 65}); } catch (_e) { rejected = true; }
  check(rejected && calls.length === callsBeforeFull, '65th pending operation reached fetch');
  pendingMap = JSON.parse(storage.get(pendingEntry[0]));
  check(Object.keys(pendingMap).length === 64 && !Object.prototype.hasOwnProperty.call(pendingMap, 'not-json'),
    'pending limit did not clean invalid entries or evicted a valid key');
  check(pendingMap[JSON.stringify(['/api/message', {slot: 1}])] === (1).toString(16).padStart(32, '0'),
    'pending limit evicted an existing retry key');

  const beforeCrossOrigin = calls.length;
  rejected = false;
  try { await ctx.apiFetch('https://evil.invalid/api/db'); } catch (_e) { rejected = true; }
  check(rejected && calls.length === beforeCrossOrigin, 'cross-origin URL was not rejected');

  global.fetch = async (url, options) => { calls.push({url, options}); return {status: 401}; };
  await ctx.apiFetch('/api/db');
  check(![...storage.values()].includes(token), '401 did not clear session token');
  const before = calls.length;
  rejected = false;
  try { await ctx.apiFetch('/api/db'); } catch (_e) { rejected = true; }
  check(rejected && calls.length === before, 'request without capability reached fetch');
  check(notices.some(x => String(x).includes('需要 capability')), 'missing explicit capability error');
  check(guard.style.display === 'flex' && guard.textContent.includes('控制动作已禁用'), 'missing persistent auth guard');
  console.log('AUTH_SMOKE_OK');
})().catch(e => { console.log('AUTH_SMOKE_FAIL ' + e.message); process.exit(1); });

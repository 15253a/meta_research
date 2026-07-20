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
const emptyState = { hidden: true };
global.window = {
  location: { hash: '#token=' + token, pathname: '/console/', search: '?console-bootstrap=1&view=live' },
  history: { replaceState(_state, _title, url) { replacements.push(url); window.location.hash = ''; } },
  sessionStorage: {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
  },
  crypto: require('crypto').webcrypto,
};
global.document = { title: 'console', getElementById(id) { return id === 'console-empty-state' ? emptyState : guard; }, createElement() { return guard; },
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
    + ' this.showEmpty=showEmptyRegistryState; this.enforceFresh=enforceSnapshotFreshness;'
    + ' this.snapshotFresh=snapshotIsFresh; this.emptyFresh=emptyRegistrySnapshotIsFresh;'
    + ' this.Hash=IncrementalSHA256; this.sha256Bytes=sha256Bytes; this.binaryPost=_reliableBinaryPost;'
    + ' this.jsonPost=_reliableJsonPost;'
    + ' this.browserPath=browserRelativePath; this.buildBrief=buildCustomGoalBrief;'
    + ' this.parseLocal=_parseLocalSourceText; this.localSummary=_localSourceSummary; this.formatPreflight=_formatPreflight;'
    + ' this.expireSnapshot=()=>{_lastDbSuccessAt=Date.now()-SNAPSHOT_MAX_AGE_MS-1;};'
    + ' this.expireEmpty=()=>{_emptyRegistrySuccessAt=Date.now()-SNAPSHOT_MAX_AGE_MS-1;};'
    + ' this.isReady=()=>_consoleReady;').call(ctx);
} catch (e) {
  console.log('AUTH_SMOKE_FAIL load: ' + e.message); process.exit(1);
}

function check(ok, message) { if (!ok) throw new Error(message); }
(async () => {
  check(replacements.length === 1 && replacements[0] === '/console/?view=live', 'fragment not cleared');
  check([...storage.values()][0] === token, 'token not stored in sessionStorage');
  check(!ctx.isReady(), 'capability alone incorrectly enabled live controls before a real DB snapshot');

  // 真实空 registry 有自己的 onboarding 遮罩：允许从 Web 新建首个任务，
  // 但不得冒充 /api/db 快照、解锁普通研究控制或露出内嵌 mock。
  ctx.showEmpty();
  check(emptyState.hidden === false && guard.style.display === 'none',
    'empty registry onboarding did not cover the mock console');
  check(!ctx.isReady() && !ctx.snapshotFresh() && ctx.emptyFresh(),
    'empty registry incorrectly became a live DB snapshot');
  ctx.enforceFresh();
  check(emptyState.hidden === false && guard.style.display === 'none',
    'fresh empty registry was overwritten by the stale DB guard');
  ctx.expireEmpty(); ctx.enforceFresh();
  check(emptyState.hidden === true && guard.style.display === 'flex'
    && guard.textContent.includes('任务列表快照已过期'),
    'stale empty registry did not fail closed');
  ctx.markFresh();

  await ctx.apiFetch('/api/db');
  check(calls.length === 1, 'api request not issued');
  check(calls[0].options.headers.get('Authorization') === 'Bearer ' + token, 'Bearer missing');
  check(calls[0].options.credentials === 'omit' && calls[0].options.redirect === 'error', 'safe fetch flags missing');

  // Whole-file hashing is genuinely incremental; browser path selection uses
  // only webkitRelativePath || name and never an Electron/host path field.
  const encoder = new TextEncoder();
  check(new ctx.Hash().digestHex() === 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    'stream SHA-256 empty vector mismatch');
  const hash = new ctx.Hash(); hash.update(encoder.encode('a')); hash.update(encoder.encode('bc'));
  check(hash.digestHex() === 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    'stream SHA-256 split vector mismatch');
  check(ctx.browserPath({webkitRelativePath:'folder/data.bin', name:'ignored.bin', path:'/secret'}) === 'folder/data.bin',
    'browser relative path did not prefer webkitRelativePath');
  let badPath = false; try { ctx.browserPath({webkitRelativePath:'', name:'C:\\secret\\data.bin'}); } catch (_e) { badPath = true; }
  check(badPath, 'host path-like browser filename was accepted');
  const brief = ctx.buildBrief('Title', 'Objective', 'Criterion', 'Constraint');
  check(brief.startsWith('---\npredicate_json: {') && brief.includes('\n---\n\n# Title'),
    'custom form did not construct valid goal brief envelope');
  const local = ctx.parseLocal('/srv/datasets/eeg\n/srv/datasets/eeg/\n', 'dataset');
  check(local.length === 1 && local[0].kind === 'dataset' && local[0].path === '/srv/datasets/eeg',
    'local source parser did not normalize/deduplicate absolute directories');
  let relativeAccepted = false;
  try { ctx.parseLocal('../private', 'references'); relativeAccepted = true; } catch (_e) {}
  check(!relativeAccepted, 'relative local source path was accepted');
  const localSummary = ctx.localSummary({source:{label:'/private/dataset', count:7, bytes:99, path:'/private/dataset'}}, 'dataset');
  const localText = ctx.formatPreflight({}, [localSummary]);
  check(localSummary.label === '数据集目录' && localSummary.count === 7 && localSummary.bytes === 99
    && !Object.prototype.hasOwnProperty.call(localSummary, 'path') && !localText.includes('/private'),
    'local source response leaked an echoed host path');

  // Ambiguous first attempt retries the exact URL/body/offset/hash with one
  // durable key.  Success echo clears that key.
  const binaryCalls = [];
  global.fetch = async (url, options) => {
    binaryCalls.push({url, options});
    if (binaryCalls.length === 1) throw new Error('connection reset');
    const key = options.headers.get('Idempotency-Key');
    return {status:200, ok:true, async json(){return {idempotency_key:'console-' + key};}};
  };
  const chunk = new Uint8Array([1,2,3,4]);
  await ctx.binaryPost('/api/quest-drafts/file/chunk?draft_id=' + '1'.repeat(32)
    + '&path=data.bin&offset=0&sha256=sha256%3Aabcd', chunk, 2);
  check(binaryCalls.length === 2, 'ambiguous binary POST was not retried');
  const firstBinaryKey = binaryCalls[0].options.headers.get('Idempotency-Key');
  check(/^[0-9a-f]{32}$/.test(firstBinaryKey)
    && binaryCalls[1].options.headers.get('Idempotency-Key') === firstBinaryKey,
    'binary retry did not preserve Idempotency-Key');
  check(binaryCalls[0].url === binaryCalls[1].url
    && binaryCalls[0].options.body === chunk && binaryCalls[1].options.body === chunk,
    'binary retry changed offset/hash URL or bytes');

  // Runtime settings may already be durable while restart scheduling still
  // needs recovery.  The precise 503 contract is non-definitive: retain the
  // operation key, then reuse and clear it only after a matching success echo.
  const runtimePayload = {quest_id:'retry-key', runtime_profile:{
    version:1, compute_profile_id:'local-gpu', review_intensity:'once'}};
  const savedPendingCalls = [];
  global.fetch = async (url, options) => {
    savedPendingCalls.push({url, options});
    const key = options.headers.get('Idempotency-Key');
    return {status:503, ok:false, async json(){return {
      error:'saved; restart pending', retryable:true,
      operation_state:'saved_pending_restart', idempotency_key:'console-' + key,
    };}};
  };
  const pendingResult = await ctx.jsonPost('/api/quest-runtime-profile', runtimePayload, 1);
  check(pendingResult.response.status === 503 && savedPendingCalls.length === 1,
    'saved-pending restart response was not returned as retryable 503');
  const savedPendingKey = savedPendingCalls[0].options.headers.get('Idempotency-Key');
  const savedPendingEntry = [...storage.entries()].find(([k]) => k.includes('pending_posts'));
  let savedPendingMap = JSON.parse(storage.get(savedPendingEntry[0]));
  check(Object.values(savedPendingMap).includes(savedPendingKey),
    'saved-pending restart response cleared its operation key');
  global.fetch = async (url, options) => {
    savedPendingCalls.push({url, options});
    const key = options.headers.get('Idempotency-Key');
    return {status:200, ok:true, async json(){return {
      ok:true, idempotency_key:'console-' + key, runtime_profile:{profile:runtimePayload.runtime_profile},
    };}};
  };
  await ctx.jsonPost('/api/quest-runtime-profile', runtimePayload, 1);
  check(savedPendingCalls[1].options.headers.get('Idempotency-Key') === savedPendingKey,
    'saved-pending restart retry did not reuse the original operation key');
  savedPendingMap = JSON.parse(storage.get(savedPendingEntry[0]));
  check(Object.keys(savedPendingMap).length === 0,
    'matching success did not clear the recovered runtime operation key');

  // A 409 remains a definitive pre-commit conflict even if a malformed body
  // copies retryable-looking fields; only the exact 503 contract keeps a key.
  global.fetch = async (url, options) => {
    const key = options.headers.get('Idempotency-Key');
    return {status:409, ok:false, async json(){return {
      error:'conflict', retryable:true,
      operation_state:'saved_pending_restart', idempotency_key:'console-' + key,
    };}};
  };
  await ctx.jsonPost('/api/quest-runtime-profile', {quest_id:'definitive-conflict',
    runtime_profile:runtimePayload.runtime_profile}, 1);
  savedPendingMap = JSON.parse(storage.get(savedPendingEntry[0]));
  check(Object.keys(savedPendingMap).length === 0,
    'definitive 409 incorrectly retained a pending operation key');
  ctx.markFresh();

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
  check(notices.some(x => String(x).includes('本机控制台授权尚未建立')), 'missing explicit capability error');
  check(guard.style.display === 'flex' && guard.textContent.includes('控制动作已禁用'), 'missing persistent auth guard');
  console.log('AUTH_SMOKE_OK');
})().catch(e => { console.log('AUTH_SMOKE_FAIL ' + e.message); process.exit(1); });

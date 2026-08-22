#!/usr/bin/env node
// Local compatibility bridge from Chrome's native CDP endpoint to the
// web-access HTTP API expected by Nature Downloader. It binds to loopback,
// attaches only to an already-running Chrome, and never reads browser storage.

import http from "node:http";
import { pathToFileURL } from "node:url";

export function parseArgs(argv) {
  const args = {
    chrome: "http://127.0.0.1:9222",
    host: "127.0.0.1",
    port: 3456,
  };
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--chrome") args.chrome = argv[++index];
    else if (value === "--host") args.host = argv[++index];
    else if (value === "--port") args.port = Number(argv[++index]);
    else throw new Error(`unknown arg ${value}`);
  }
  const upstream = new URL(args.chrome);
  if (!["127.0.0.1", "localhost", "::1"].includes(upstream.hostname)) {
    throw new Error("--chrome must use a loopback host");
  }
  if (!["127.0.0.1", "localhost", "::1"].includes(args.host)) {
    throw new Error("--host must use a loopback host");
  }
  if (!Number.isInteger(args.port) || args.port < 1 || args.port > 65535) {
    throw new Error("--port must be an integer from 1 to 65535");
  }
  args.chrome = upstream.toString().replace(/\/$/, "");
  return args;
}

async function readJson(url, options = {}, timeoutMs = 10000) {
  const response = await fetch(url, {
    ...options,
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`upstream HTTP ${response.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : {};
}

export async function chromeTargets(chrome) {
  return readJson(`${chrome}/json/list`);
}

function publicTarget(target) {
  return {
    targetId: target.id,
    type: target.type,
    title: target.title || "",
    url: target.url || "",
  };
}

async function findTarget(chrome, targetId) {
  const target = (await chromeTargets(chrome)).find((item) => item.id === targetId);
  if (!target) throw new Error(`unknown target ${targetId}`);
  if (!target.webSocketDebuggerUrl) throw new Error(`target ${targetId} has no CDP websocket`);
  return target;
}

export async function openCdpSession(chrome, targetId, connectTimeoutMs = 10000) {
  const target = await findTarget(chrome, targetId);
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`CDP websocket connection timed out for ${targetId}`)), connectTimeoutMs);
    socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error(`CDP websocket failed for ${targetId}`)); }, { once: true });
  });
  let nextId = 1;
  let closed = false;
  const pending = new Map();
  const rejectPending = (message) => {
    for (const item of pending.values()) {
      clearTimeout(item.timer);
      item.reject(new Error(message));
    }
    pending.clear();
  };
  socket.addEventListener("message", (event) => {
    let payload;
    try { payload = JSON.parse(String(event.data)); } catch (_) { return; }
    const item = pending.get(payload.id);
    if (!item) return;
    pending.delete(payload.id);
    clearTimeout(item.timer);
    if (payload.error) item.reject(new Error(payload.error.message || `CDP ${item.method} failed`));
    else item.resolve(payload.result || {});
  });
  socket.addEventListener("close", () => {
    closed = true;
    rejectPending(`CDP websocket closed for ${targetId}`);
  });
  socket.addEventListener("error", () => rejectPending(`CDP websocket failed for ${targetId}`));
  return {
    call(method, params = {}, timeoutMs = 60000) {
      if (closed || socket.readyState !== WebSocket.OPEN) return Promise.reject(new Error(`CDP session is closed for ${targetId}`));
      const id = nextId++;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`CDP ${method} timed out`));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer, method });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      if (closed) return;
      closed = true;
      rejectPending(`CDP session closed for ${targetId}`);
      try { socket.close(); } catch (_) {}
    },
  };
}

export async function cdpCall(chrome, targetId, method, params = {}, timeoutMs = 60000) {
  const session = await openCdpSession(chrome, targetId, Math.min(timeoutMs, 10000));
  try {
    return await session.call(method, params, timeoutMs);
  } finally {
    session.close();
  }
}

export async function evaluate(chrome, targetId, expression, timeoutMs = 60000) {
  const payload = await cdpCall(chrome, targetId, "Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  }, timeoutMs);
  if (payload.exceptionDetails) {
    const message = payload.exceptionDetails.exception?.description || payload.exceptionDetails.text || "evaluation failed";
    throw new Error(message.slice(0, 500));
  }
  return payload.result?.value ?? null;
}

async function readBody(request, maxBytes = 2 * 1024 * 1024) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > maxBytes) throw new Error("request body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function sendJson(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

export function createProxyServer(args) {
  return http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, `http://${args.host}:${args.port}`);
      const targetId = url.searchParams.get("target");
      if (request.method === "GET" && url.pathname === "/targets") {
        sendJson(response, 200, (await chromeTargets(args.chrome)).map(publicTarget));
        return;
      }
      if (request.method === "GET" && url.pathname === "/info") {
        const target = await findTarget(args.chrome, targetId);
        const ready = await evaluate(args.chrome, targetId, "document.readyState").catch(() => null);
        sendJson(response, 200, { ...publicTarget(target), ready });
        return;
      }
      if (request.method === "POST" && url.pathname === "/new") {
        const destination = await readBody(request);
        const created = await readJson(`${args.chrome}/json/new?${encodeURIComponent(destination)}`, { method: "PUT" });
        sendJson(response, 200, publicTarget(created));
        return;
      }
      if (request.method === "POST" && url.pathname === "/navigate") {
        if (!targetId) throw new Error("navigate needs target");
        const destination = await readBody(request);
        await cdpCall(args.chrome, targetId, "Page.navigate", { url: destination });
        sendJson(response, 200, { targetId, url: destination });
        return;
      }
      if (request.method === "POST" && url.pathname === "/eval") {
        if (!targetId) throw new Error("eval needs target");
        const expression = await readBody(request);
        sendJson(response, 200, { targetId, value: await evaluate(args.chrome, targetId, expression) });
        return;
      }
      if (request.method === "POST" && url.pathname === "/click") {
        if (!targetId) throw new Error("click needs target");
        const selector = await readBody(request);
        const expression = `(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;e.click();return true;})()`;
        sendJson(response, 200, { targetId, value: await evaluate(args.chrome, targetId, expression) });
        return;
      }
      if (request.method === "GET" && url.pathname === "/scroll") {
        if (!targetId) throw new Error("scroll needs target");
        const direction = url.searchParams.get("direction") || "bottom";
        const expression = direction === "top"
          ? "(()=>{window.scrollTo(0,0);return window.scrollY;})()"
          : "(()=>{window.scrollTo(0,document.documentElement.scrollHeight);return window.scrollY;})()";
        sendJson(response, 200, { targetId, value: await evaluate(args.chrome, targetId, expression) });
        return;
      }
      if (request.method === "GET" && url.pathname === "/close") {
        if (!targetId) throw new Error("close needs target");
        const closed = await fetch(`${args.chrome}/json/close/${encodeURIComponent(targetId)}`, {
          signal: AbortSignal.timeout(10000),
        });
        const message = await closed.text();
        if (!closed.ok) throw new Error(`upstream HTTP ${closed.status}: ${message.slice(0, 300)}`);
        sendJson(response, 200, { targetId, message });
        return;
      }
      sendJson(response, 404, { error: "not_found" });
    } catch (error) {
      sendJson(response, 500, { error: String(error?.message || error).slice(0, 800) });
    }
  });
}

async function main() {
  const args = parseArgs(process.argv);
  const version = await readJson(`${args.chrome}/json/version`);
  const targets = await chromeTargets(args.chrome);
  const server = createProxyServer(args);
  server.listen(args.port, args.host, () => {
    console.log(JSON.stringify({
      ok: true,
      proxy: `http://${args.host}:${args.port}`,
      chrome: args.chrome,
      browser: version.Browser || null,
      targets: targets.filter((item) => item.type === "page").length,
    }));
  });
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.close(() => process.exit(0)));
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  main().catch((error) => {
    console.error(JSON.stringify({ ok: false, error: String(error?.message || error) }));
    process.exit(2);
  });
}

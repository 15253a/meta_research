#!/usr/bin/env node
// Download an institution-authorized response through Chrome's CDP network
// loader. Use when the page is open in the authenticated browser but a normal
// page fetch is blocked by CORS or a WebVPN one-time response.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { evaluate, openCdpSession } from "./direct_cdp_proxy.mjs";

export function parseArgs(argv) {
  const args = {
    chrome: "http://127.0.0.1:9222",
    timeout: 120,
    maxBytes: 100 * 1024 * 1024,
  };
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--target") args.target = argv[++index];
    else if (value === "--url") args.url = argv[++index];
    else if (value === "--out") args.out = argv[++index];
    else if (value === "--expect-title") args.expectTitle = argv[++index];
    else if (value === "--chrome") args.chrome = argv[++index];
    else if (value === "--timeout") args.timeout = Number(argv[++index]);
    else if (value === "--max-bytes") args.maxBytes = Number(argv[++index]);
    else throw new Error(`unknown arg ${value}`);
  }
  for (const key of ["target", "url", "out"]) {
    if (!args[key]) throw new Error(`--${key} is required`);
  }
  if (!Number.isFinite(args.timeout) || args.timeout < 5 || args.timeout > 300) {
    throw new Error("--timeout must be from 5 to 300 seconds");
  }
  if (!Number.isInteger(args.maxBytes) || args.maxBytes < 1024 || args.maxBytes > 1024 * 1024 * 1024) {
    throw new Error("--max-bytes must be from 1024 to 1073741824");
  }
  const upstream = new URL(args.chrome);
  if (!["127.0.0.1", "localhost", "::1"].includes(upstream.hostname)) {
    throw new Error("--chrome must use a loopback host");
  }
  const source = new URL(args.url);
  if (!["http:", "https:"].includes(source.protocol)) throw new Error("--url must be HTTP(S)");
  args.chrome = upstream.toString().replace(/\/$/, "");
  args.out = path.resolve(args.out);
  return args;
}

function normalized(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

export function titleMatches(actual, expected) {
  if (!expected) return true;
  const actualTokens = new Set(normalized(actual).split(" ").filter((item) => item.length >= 4));
  const expectedTokens = normalized(expected).split(" ").filter((item) => item.length >= 4);
  if (!expectedTokens.length) return normalized(actual).includes(normalized(expected));
  return expectedTokens.filter((item) => actualTokens.has(item)).length / expectedTokens.length >= 0.7;
}

async function main() {
  const args = parseArgs(process.argv);
  const actualTitle = await evaluate(args.chrome, args.target, "document.title", args.timeout * 1000);
  if (!titleMatches(actualTitle, args.expectTitle)) {
    throw new Error(`browser page title mismatch: ${actualTitle || "unknown"}`);
  }
  const session = await openCdpSession(args.chrome, args.target, Math.min(args.timeout * 1000, 10000));
  let resource;
  try {
    const tree = await session.call("Page.getFrameTree", {}, args.timeout * 1000);
    const frameId = tree.frameTree?.frame?.id;
    if (!frameId) throw new Error("browser page has no main frame");
    const loaded = await session.call("Network.loadNetworkResource", {
      frameId,
      url: args.url,
      options: { disableCache: false, includeCredentials: true },
    }, args.timeout * 1000);
    resource = loaded.resource || {};
    if (!resource.success || resource.httpStatusCode < 200 || resource.httpStatusCode >= 300 || !resource.stream) {
      throw new Error(`browser network load failed: HTTP ${resource.httpStatusCode || "?"}, net=${resource.netError ?? "?"}`);
    }

  const parent = path.dirname(args.out);
  fs.mkdirSync(parent, { recursive: true });
  const tempDir = fs.mkdtempSync(path.join(parent, ".nature-cdp-stream-"));
  const tempFile = path.join(tempDir, "download.bin");
  try {
  const descriptor = fs.openSync(tempFile, "wx");
  const hash = crypto.createHash("sha256");
  let bytes = 0;
  let head = Buffer.alloc(0);
  try {
    while (true) {
      const part = await session.call("IO.read", {
        handle: resource.stream,
        size: 524288,
      }, args.timeout * 1000);
      const chunk = Buffer.from(part.data || "", part.base64Encoded ? "base64" : "utf8");
      bytes += chunk.length;
      if (bytes > args.maxBytes) throw new Error("browser response exceeds --max-bytes");
      if (head.length < 8) head = Buffer.concat([head, chunk]).subarray(0, 8);
      fs.writeSync(descriptor, chunk);
      hash.update(chunk);
      if (part.eof) break;
    }
  } finally {
    fs.closeSync(descriptor);
    if (resource?.stream) await session.call("IO.close", { handle: resource.stream }, 10000).catch(() => {});
  }
  if (!head.toString("ascii").startsWith("%PDF")) {
    throw new Error(`browser response is not PDF: ${head.toString("ascii")}`);
  }
  const digest = hash.digest("hex");
  if (fs.existsSync(args.out)) {
    const existing = crypto.createHash("sha256").update(fs.readFileSync(args.out)).digest("hex");
    if (existing !== digest) throw new Error(`output already exists with different content: ${args.out}`);
    fs.unlinkSync(tempFile);
  } else {
    fs.renameSync(tempFile, args.out);
  }
  fs.rmdirSync(tempDir);
  console.log(JSON.stringify({
    ok: true,
    status: "downloaded",
    out: args.out,
    bytes,
    sha256: digest,
    title: actualTitle || null,
    sourceUrl: args.url,
    httpStatusCode: resource.httpStatusCode,
    accessMode: "institution_browser_cdp_stream",
  }, null, 2));
  } finally {
    if (fs.existsSync(tempFile)) fs.unlinkSync(tempFile);
    if (fs.existsSync(tempDir)) fs.rmdirSync(tempDir);
  }
  } finally {
    session.close();
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  main().catch((error) => {
    console.error(error.stack || String(error));
    process.exit(1);
  });
}

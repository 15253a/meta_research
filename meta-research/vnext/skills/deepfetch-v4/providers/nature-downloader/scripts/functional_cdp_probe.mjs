#!/usr/bin/env node

import { closeTab, evalJs, healthCheck, navigate, newTab } from "./lib/cdp-utils.mjs";

function proxyArg(argv) {
  const index = argv.indexOf("--proxy");
  return index >= 0 ? argv[index + 1] : "http://127.0.0.1:3456";
}

function reportFailure(stage, error) {
  console.log(JSON.stringify({
    ok: false,
    stage,
    error: String(error?.message || error).slice(0, 160),
  }));
}

async function atStage(stage, action) {
  try {
    return await action();
  } catch (error) {
    error.probeStage = stage;
    throw error;
  }
}

async function main() {
  const proxy = proxyArg(process.argv);
  let target = "";
  try {
    await atStage("targets", () => healthCheck(proxy));
    const opened = await atStage("new", () => newTab(proxy, "about:blank", 10000));
    target = opened?.targetId || "";
    if (!target) {
      const error = new Error("new tab returned no targetId");
      error.probeStage = "new";
      throw error;
    }
    await atStage("navigate", () =>
      navigate(proxy, target, "data:text/html,cdp-probe", 10000)
    );
    const value = await atStage("eval", () => evalJs(proxy, target, "1+1", 10000));
    if (value !== 2) {
      const error = new Error("Runtime.evaluate returned an unexpected value");
      error.probeStage = "eval";
      throw error;
    }
    console.log(JSON.stringify({ ok: true, proxy }));
    return 0;
  } catch (error) {
    reportFailure(error.probeStage || "unknown", error);
    return 2;
  } finally {
    if (target) await closeTab(proxy, target);
  }
}

process.exitCode = await main();

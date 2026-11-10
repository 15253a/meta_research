import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

// Exercise the production API against real browser IDB, crypto and Web Locks.
// Only HTTP delivery is controlled; no storage implementation is substituted.
declare global {
  interface Window {
    recoveryApi: typeof import("../src/api");
    migrationTransactions: number;
  }
}
const origin = "http://localhost:18774";
const legacySlot = "meta_research_pending_human_request_response";
const requestRef = "research_graph:HR-storage:r1";
const manifestKey = `meta-research/human-request-recovery/v2:response:${encodeURIComponent(requestRef)}`;

async function install(page: Page) {
  const source = await readFile(process.env.META_RESEARCH_TEST_API_SOURCE ?? new URL("../src/api.ts", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
  await page.context().addCookies([{ name: "meta_research_csrf", value: "storage-test", url: origin }]);
  await page.context().route(`${origin}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api.js") return route.fulfill({ contentType: "application/javascript", body: code });
    if (path.startsWith("/api/")) return route.abort("connectionreset");
    return route.fulfill({ contentType: "text/html", body: '<script type="module">import * as api from "/api.js"; window.recoveryApi = api;</script>' });
  });
  await open(page);
}
async function open(page: Page) {
  await page.goto(origin);
  await page.waitForFunction(() => Boolean(window.recoveryApi));
}
async function draft(page: Page, ref = requestRef, note = "sealed original") {
  return page.evaluate(async ({ ref, note }) => {
    try { await window.recoveryApi.respondToHumanRequest(ref, { decision: "provided", facts: {}, note }); }
    catch (error) { return (error as Error).message; }
    return null;
  }, { ref, note });
}
async function state(page: Page) {
  return page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("meta_research_human_request_recovery", 3);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      return await new Promise<{ keys: IDBValidKey[]; ciphertexts: IDBValidKey[]; manifests: Record<string, string>; session: Record<string, string> }>(resolve => {
        const tx = db.transaction(["sealed_response_keys", "sealed_payloads", "recovery_manifests"], "readonly");
        const keys = tx.objectStore("sealed_response_keys").getAllKeys();
        const ciphertexts = tx.objectStore("sealed_payloads").getAllKeys();
        const names = tx.objectStore("recovery_manifests").getAllKeys();
        const values = tx.objectStore("recovery_manifests").getAll();
        tx.oncomplete = () => resolve({ keys: keys.result, ciphertexts: ciphertexts.result,
          manifests: Object.fromEntries(names.result.map((key, i) => [String(key), values.result[i]])), session: { ...sessionStorage } });
      });
    } finally { db.close(); }
  });
}
async function moveToLegacy(page: Page, location: "session" | "idb", damage?: "key" | "cipher" | "json") {
  return page.evaluate(async ({ location, damage, legacySlot, manifestKey }) => {
    const db = await new Promise<IDBDatabase>(resolve => { const r = indexedDB.open("meta_research_human_request_recovery"); r.onsuccess = () => resolve(r.result); });
    try {
      return await new Promise<string>((resolve, reject) => {
        const tx = db.transaction(["recovery_manifests", "sealed_response_keys", "sealed_payloads"], "readwrite");
        const store = tx.objectStore("recovery_manifests");
        const get = store.get(manifestKey); let serialized = "";
        get.onsuccess = () => {
          serialized = get.result as string;
          const sealed = JSON.parse(serialized).sealed_response;
          if (damage === "key") tx.objectStore("sealed_response_keys").delete(sealed.key_ref);
          if (damage === "cipher") tx.objectStore("sealed_payloads").put(new Uint8Array([1, 2, 3]).buffer, sealed.ciphertext_ref);
          if (damage === "json") serialized = "{corrupt";
          store.delete(manifestKey);
          if (location === "idb") store.put(serialized, legacySlot);
        };
        tx.oncomplete = () => { if (location === "session") sessionStorage.setItem(legacySlot, serialized); else sessionStorage.removeItem(legacySlot); resolve(serialized); };
        tx.onabort = () => reject(tx.error);
      });
    } finally { db.close(); }
  }, { location, damage, legacySlot, manifestKey });
}
async function hydrate(page: Page) {
  return page.evaluate(async () => { try { await window.recoveryApi.hydratePendingHumanRequestRecovery(); return null; } catch (error) { return (error as Error).message; } });
}

test("new per-request drafts reload across tabs without legacy migration transactions", async ({ page }, info) => {
  await install(page);
  await draft(page); await draft(page, "research_graph:HR-second:r1", "second exact body");
  const before = await state(page);
  expect(Object.keys(before.manifests)).toHaveLength(2);
  expect(Object.keys(before.session).filter(key => key.startsWith("meta_research_pending_write:"))).toHaveLength(2);
  await page.reload(); await page.waitForFunction(() => Boolean(window.recoveryApi));
  await page.evaluate(() => {
    window.migrationTransactions = 0;
    const transaction = IDBDatabase.prototype.transaction;
    IDBDatabase.prototype.transaction = function (...args: Parameters<typeof transaction>) {
      if (args[1] === "readwrite" && new Error().stack?.includes("migrateLegacyHumanRequestRecoveryManifest")) window.migrationTransactions++;
      return transaction.apply(this, args);
    };
  });
  for (let i = 0; i < 4; i++) expect(await hydrate(page)).toBeNull();
  const migrations = await page.evaluate(() => window.migrationTransactions);
  await info.attach("migration-cost", { body: JSON.stringify({ hydrations: 4, migrations }), contentType: "application/json" });
  expect(migrations).toBe(0);
  expect(before.session[legacySlot]).toBeUndefined();
  const other = await page.context().newPage(); await open(other); expect(await hydrate(other)).toBeNull();
  expect(await other.evaluate(ref => window.recoveryApi.pendingHumanRequestResponsePreview(ref), requestRef)).toEqual({ decision: "provided", facts: {}, note: "sealed original" });
  expect((await state(other)).manifests).toEqual(before.manifests);
});

for (const location of ["session", "idb"] as const) {
  test(`genuine ${location} legacy is consumed only after durable import and not recreated`, async ({ page }) => {
    await install(page); await draft(page); const original = await moveToLegacy(page, location);
    expect(await hydrate(page)).toBeNull();
    const imported = await state(page);
    expect(imported.manifests[manifestKey]).toBe(original);
    expect(imported.manifests[legacySlot]).toBeUndefined(); expect(imported.session[legacySlot]).toBeUndefined();
    expect(await hydrate(page)).toBeNull(); expect((await state(page)).manifests).toEqual(imported.manifests);
  });
  for (const damage of ["key", "cipher", "json"] as const) {
    test(`${location} legacy with damaged ${damage} retains its original and reports failure`, async ({ page }) => {
      await install(page); await draft(page); await moveToLegacy(page, location, damage);
      const before = await state(page);
      expect(await hydrate(page)).toMatch(/invalid|missing/);
      expect(await state(page)).toEqual(before);
    });
  }
}

test("an aborted legacy import keeps the source for a successful retry", async ({ page }) => {
  await install(page); await draft(page); const original = await moveToLegacy(page, "session");
  await page.evaluate(() => {
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (...args: Parameters<typeof put>) {
      const result = put.apply(this, args);
      if (this.name === "recovery_manifests") { IDBObjectStore.prototype.put = put; this.transaction.abort(); }
      return result;
    };
  });
  expect(await hydrate(page)).toContain("store_unavailable");
  expect((await state(page)).session[legacySlot]).toBe(original);
  expect((await state(page)).manifests[manifestKey]).toBeUndefined();
  expect(await hydrate(page)).toBeNull(); expect((await state(page)).manifests[manifestKey]).toBe(original);
});

test("conflicting legacy slots preserve both originals instead of overwriting either", async ({ page }) => {
  await install(page); await draft(page); const original = (await state(page)).manifests[manifestKey];
  await page.evaluate(({ legacySlot, original }) => {
    // Both manifests pass the cryptographic binding; the durable CAS must
    // still refuse to replace a different serialized original.
    sessionStorage.setItem(legacySlot, JSON.stringify(JSON.parse(original), null, 2));
  }, { legacySlot, original });
  const before = await state(page); expect(await hydrate(page)).toContain("manifest_conflict");
  expect(await state(page)).toEqual(before);
});

test("same-request cross-tab submission cannot overwrite a sealed pending draft", async ({ page }) => {
  await install(page); await draft(page); const before = await state(page);
  const other = await page.context().newPage(); await open(other);
  expect(await draft(other, requestRef, "conflicting tab input")).toContain("recovery_conflict");
  expect((await state(other)).manifests).toEqual(before.manifests);
});

test("old material imports remain reselection notices without sending old intake", async ({ page }) => {
  await install(page);
  await page.evaluate(() => sessionStorage.setItem("meta_research_pending_human_request_asset_intake_operation", JSON.stringify({ schema: "meta-research/human-request-asset-intake/v1", request_ref: "research_graph:HR-storage:r1", sealed_operation: { storage_ref: "old-cipher" } })));
  expect(await hydrate(page)).toBeNull();
  expect(await page.evaluate(ref => window.recoveryApi.hasLegacyHumanRequestMaterial(ref), requestRef)).toBe(true);
  expect((await state(page)).session.meta_research_pending_human_request_asset_intake_operation).toBeUndefined();
});

for (const damage of ["key", "cipher", "json"] as const) {
  test(`a current per-request record with damaged ${damage} is retained with its remaining bytes`, async ({ page }) => {
    await install(page); await draft(page);
    await moveToLegacy(page, "idb", damage);
    await page.evaluate(async ({ legacySlot, manifestKey }) => {
      const db = await new Promise<IDBDatabase>(resolve => { const r = indexedDB.open("meta_research_human_request_recovery"); r.onsuccess = () => resolve(r.result); });
      await new Promise<void>(resolve => {
        const tx = db.transaction("recovery_manifests", "readwrite"); const store = tx.objectStore("recovery_manifests"); const get = store.get(legacySlot);
        get.onsuccess = () => { store.put(get.result, manifestKey); store.delete(legacySlot); };
        tx.oncomplete = () => resolve();
      });
      db.close();
    }, { legacySlot, manifestKey });
    const before = await state(page);
    const error = await page.evaluate(async ref => {
      try { await window.recoveryApi.pendingHumanRequestResponsePreview(ref); return null; }
      catch (error) { return (error as Error).message; }
    }, requestRef);
    expect(error).toMatch(/invalid|missing/); expect(await state(page)).toEqual(before);
  });
}

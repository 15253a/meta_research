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
      return await new Promise<{ keys: IDBValidKey[]; ciphertexts: IDBValidKey[]; ciphertextBytes: Record<string, number[]>; manifests: Record<string, string>; session: Record<string, string> }>(resolve => {
        const tx = db.transaction(["sealed_response_keys", "sealed_payloads", "recovery_manifests"], "readonly");
        const keys = tx.objectStore("sealed_response_keys").getAllKeys();
        const ciphertexts = tx.objectStore("sealed_payloads").getAllKeys();
        const ciphertextValues = tx.objectStore("sealed_payloads").getAll();
        const names = tx.objectStore("recovery_manifests").getAllKeys();
        const values = tx.objectStore("recovery_manifests").getAll();
        tx.oncomplete = () => resolve({ keys: keys.result, ciphertexts: ciphertexts.result,
          ciphertextBytes: Object.fromEntries(ciphertexts.result.map((key, i) => [String(key), Array.from(new Uint8Array(ciphertextValues.result[i]))])),
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

for (const damage of ["key", "cipher", "json", "binding", "legacy-json", "legacy-conflict", "legacy-identifiable", "legacy-idb-key", "legacy-idb-cipher", "legacy-idb-json", "legacy-idb-conflict", "alias-key", "invalid-body"] as const) {
  test(`damaged A ${damage} leaves B and new C usable across reload and tabs`, async ({ page }) => {
    await install(page);
    const b = "research_graph:HR-healthy:r1", c = "research_graph:HR-new:r1";
    if (damage === "invalid-body") {
      await page.evaluate(async ref => {
        try { await window.recoveryApi.respondToHumanRequest(ref, { decision: "provided", facts: null as unknown as Record<string, unknown>, note: "invalid authenticated body" }); } catch {}
      }, requestRef);
    } else await draft(page);
    await draft(page, b, "healthy exact body");
    await page.evaluate(async ({ manifestKey, legacySlot, damage }) => {
      const db = await new Promise<IDBDatabase>(resolve => { const r = indexedDB.open("meta_research_human_request_recovery"); r.onsuccess = () => resolve(r.result); });
      await new Promise<void>(resolve => {
        const tx = db.transaction(["recovery_manifests", "sealed_response_keys", "sealed_payloads"], "readwrite");
        const store = tx.objectStore("recovery_manifests"), get = store.get(manifestKey);
        get.onsuccess = () => {
          const value = JSON.parse(get.result);
          const recordDamage = damage.replace("legacy-idb-", "");
          if (damage === "alias-key") {
            const other = store.get("meta-research/human-request-recovery/v2:response:" + encodeURIComponent("research_graph:HR-healthy:r1"));
            other.onsuccess = () => { value.sealed_response.key_ref = JSON.parse(other.result).sealed_response.key_ref; store.put(JSON.stringify(value), manifestKey); };
          }
          if (recordDamage === "key") tx.objectStore("sealed_response_keys").delete(value.sealed_response.key_ref);
          if (recordDamage === "cipher") tx.objectStore("sealed_payloads").put(new Uint8Array([1, 2, 3]).buffer, value.sealed_response.ciphertext_ref);
          if (damage === "json") store.put("{broken", manifestKey);
          if (damage === "binding") { value.response_idempotency_key = "forged"; store.put(JSON.stringify(value), manifestKey); }
          if (damage === "legacy-json") sessionStorage.setItem(legacySlot, "{broken");
          if (damage === "legacy-conflict") sessionStorage.setItem(legacySlot, JSON.stringify(value, null, 2));
          if (damage === "legacy-identifiable") sessionStorage.setItem(legacySlot, JSON.stringify({ schema: value.schema, request_ref: value.request_ref }));
          if (damage.startsWith("legacy-idb-")) {
            store.put(recordDamage === "json" ? "{broken" : recordDamage === "conflict" ? JSON.stringify(value, null, 2) : get.result, legacySlot);
            if (recordDamage !== "conflict") store.delete(manifestKey);
          }
        };
        tx.oncomplete = () => resolve();
      }); db.close();
    }, { manifestKey, legacySlot, damage });
    const before = await state(page);
    await page.reload(); await page.waitForFunction(() => Boolean(window.recoveryApi));
    const bodies: unknown[] = [];
    await page.context().route(`${origin}/api/**`, async route => { bodies.push(route.request().postDataJSON()); await route.abort("connectionreset"); });
    const other = await page.context().newPage(); await open(other);
    for (const tab of [page, other]) {
      expect(await tab.evaluate(ref => window.recoveryApi.pendingHumanRequestResponsePreview(ref), b)).toEqual({ decision: "provided", facts: {}, note: "healthy exact body" });
      await tab.evaluate(async ref => { try { await window.recoveryApi.deliverPendingHumanRequestResponse(ref); } catch {} }, b);
    }
    expect(bodies).toEqual(Array(2).fill({ decision: "provided", facts: {}, note: "healthy exact body" }));
    await draft(page, c, "new exact body");
    expect(bodies[2]).toEqual({ decision: "provided", facts: {}, note: "new exact body" });
    expect((await state(page)).manifests[`meta-research/human-request-recovery/v2:response:${encodeURIComponent(c)}`]).toContain('"request_ref":"research_graph:HR-new:r1"');
    if (damage !== "legacy-json" && damage !== "legacy-idb-json") {
      const error = await page.evaluate(async ref => { try { await window.recoveryApi.pendingHumanRequestResponsePreview(ref); return null; } catch (error) { return (error as Error).message; } }, requestRef);
      const expectedError = damage === "alias-key" ? "human_request_recovery_seal_invalid"
        : damage === "invalid-body" ? "human_request_asset_response_body_invalid"
        : damage.endsWith("key") ? "human_request_recovery_sealing_key_missing"
        : damage.endsWith("cipher") ? "human_request_recovery_seal_invalid"
        : damage.endsWith("conflict") ? "human_request_recovery_manifest_conflict"
        : damage === "binding" ? "human_request_recovery_binding_invalid"
        : damage === "legacy-identifiable" ? "human_request_response_recovery_invalid"
        : "human_request_recovery_manifest_invalid";
      expect(error).toBe(expectedError);
      expect(await draft(other, requestRef, "must not replace A")).toMatch(/invalid|missing|conflict/);
    }
    expect(await hydrate(page)).toMatch(/invalid|missing|conflict/);
    const after = await state(page);
    expect(after.manifests[manifestKey]).toBe(before.manifests[manifestKey]);
    expect(after.session[legacySlot]).toBe(before.session[legacySlot]);
    expect(after.manifests[legacySlot]).toBe(before.manifests[legacySlot]);
    for (const [ref, bytes] of Object.entries(before.ciphertextBytes)) expect(after.ciphertextBytes[ref]).toEqual(bytes);
    expect(after.keys).toEqual(expect.arrayContaining(before.keys));
    expect(after.ciphertexts).toEqual(expect.arrayContaining(before.ciphertexts));
  });
}


test("a real recovery transaction abort is global and a retry reopens healthy records", async ({ page }) => {
  await install(page); await draft(page);
  const before = await state(page);
  await page.evaluate(() => {
    const getAll = IDBObjectStore.prototype.getAll;
    IDBObjectStore.prototype.getAll = function (...args: Parameters<typeof getAll>) {
      const request = getAll.apply(this, args);
      if (this.name === "recovery_manifests") { IDBObjectStore.prototype.getAll = getAll; this.transaction.abort(); }
      return request;
    };
  });
  expect(await draft(page, "research_graph:HR-new:r1", "blocked by actual store failure")).toBe("human_request_recovery_manifest_store_unavailable");
  expect(await state(page)).toEqual(before);
  expect(await page.evaluate(ref => window.recoveryApi.pendingHumanRequestResponsePreview(ref), requestRef)).toEqual({ decision: "provided", facts: {}, note: "sealed original" });
});

test("repairing durable ciphertext clears only that request diagnostic on retry", async ({ page }) => {
  await install(page); await draft(page);
  const original = await state(page);
  await page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>(resolve => { const r = indexedDB.open("meta_research_human_request_recovery"); r.onsuccess = () => resolve(r.result); });
    await new Promise<void>(resolve => {
      const tx = db.transaction("sealed_payloads", "readwrite"), store = tx.objectStore("sealed_payloads"), keys = store.getAllKeys();
      keys.onsuccess = () => store.put(new Uint8Array([1, 2, 3]).buffer, keys.result[0]); tx.oncomplete = () => resolve();
    }); db.close();
  });
  expect(await hydrate(page)).toBe("human_request_recovery_seal_invalid");
  expect(await draft(page, requestRef, "replacement is forbidden")).toBe("human_request_recovery_seal_invalid");
  await page.evaluate(async bytes => {
    const db = await new Promise<IDBDatabase>(resolve => { const r = indexedDB.open("meta_research_human_request_recovery"); r.onsuccess = () => resolve(r.result); });
    await new Promise<void>(resolve => {
      const tx = db.transaction("sealed_payloads", "readwrite");
      for (const [ref, value] of Object.entries(bytes)) tx.objectStore("sealed_payloads").put(new Uint8Array(value).buffer, ref);
      tx.oncomplete = () => resolve();
    }); db.close();
  }, original.ciphertextBytes);
  expect(await page.evaluate(ref => window.recoveryApi.pendingHumanRequestResponsePreview(ref), requestRef)).toEqual({ decision: "provided", facts: {}, note: "sealed original" });
  expect(await state(page)).toEqual(original);
});

test("successful B and C delivery retain keys reachable from corrupt A", async ({ page }) => {
  await install(page);
  const b = "research_graph:HR-healthy:r1", c = "research_graph:HR-new:r1";
  await draft(page); await draft(page, b, "healthy exact body");
  const original = await state(page);
  const aCipher = JSON.parse(original.manifests[manifestKey]).sealed_response.ciphertext_ref as string;
  await page.evaluate(async ({ manifestKey, b }) => {
    const db = await new Promise<IDBDatabase>(resolve => { const r = indexedDB.open("meta_research_human_request_recovery"); r.onsuccess = () => resolve(r.result); });
    await new Promise<void>(resolve => {
      const tx = db.transaction("recovery_manifests", "readwrite"), store = tx.objectStore("recovery_manifests");
      const a = store.get(manifestKey), other = store.get("meta-research/human-request-recovery/v2:response:" + encodeURIComponent(b));
      other.onsuccess = () => { const value = JSON.parse(a.result); value.sealed_response.key_ref = JSON.parse(other.result).sealed_response.key_ref; store.put(JSON.stringify(value), manifestKey); };
      tx.oncomplete = () => resolve();
    }); db.close();
  }, { manifestKey, b });
  const damaged = await state(page);
  const bodies: unknown[] = [];
  await page.context().route(`${origin}/api/**`, async route => { bodies.push(route.request().postDataJSON()); await route.fulfill({ json: { response_ref: "recorded" } }); });
  expect(await page.evaluate(ref => window.recoveryApi.pendingHumanRequestResponsePreview(ref), b)).toEqual({ decision: "provided", facts: {}, note: "healthy exact body" });
  expect(await page.evaluate(ref => window.recoveryApi.deliverPendingHumanRequestResponse(ref), b)).toEqual({ response_ref: "recorded" });
  expect(await draft(page, c, "new exact body")).toBeNull();
  expect(bodies).toEqual([{ decision: "provided", facts: {}, note: "healthy exact body" }, { decision: "provided", facts: {}, note: "new exact body" }]);
  const after = await state(page);
  expect(after.manifests[manifestKey]).toBe(damaged.manifests[manifestKey]);
  expect(after.ciphertextBytes[aCipher]).toEqual(original.ciphertextBytes[aCipher]);
  expect(after.keys).toEqual(expect.arrayContaining(original.keys));
  expect(await draft(page, requestRef, "must not replace")).toBe("human_request_recovery_seal_invalid");
});

import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { QuestCreationView } from "../src/api";

test("DeepFetch streams material understanding before generation, then changes producers without stale activity or premature completion", async ({ page }, testInfo) => {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const hash = "b".repeat(64);
  const requestRef = "deepfetch-material-preparation";
  const generationRef = "generation-after-deepfetch";
  const startedAt = 1_791_705_600;
  await page.clock.setFixedTime(new Date((startedAt + 65) * 1000));
  const accepted = (kind: string, subject: string) => ({
    status: "accepted" as const, issuer: "fixture", kind, receipt_ref: `receipt-${kind}`, subject_ref: subject, payload_hash: hash,
  });
  const creation: QuestCreationView = {
    initialization_id: "init-preparation", creation_context: "quest_initialization", route: "deepfetch", status: "proposal_generating",
    quest_draft: { revision: 3, hash, schema_ref: "meta-research/quest-initialization-draft/v2", value: {
      goal: "根据已有研究材料准备第一问", completion_criteria: "说明有依据的待解决问题", time_budget: "2h", research_style: "balanced", route: "deepfetch",
      resource_envelope_ref: null, resource_envelope_hash: null, background_and_initial_direction: "",
      literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
      material_manifest: { entries: [{ material_key: "research-notes", relative_path: "research-notes.md", sha256: hash, bytes: 120 }], submissions: [] },
    } },
    compute: null, resource_envelope: null, creation_basis: null,
    proposal_generation: { ref: "old-failed-generation", basis_revision: 2, basis_hash: "a".repeat(64), status: "failed", adapter_kind: "production",
      attempt_count: 1, created_at: startedAt - 90, started_at: startedAt - 90, completed_at: startedAt - 30,
      failure: { code: "protected_creation_launch_failed" }, literature_snapshot_ref: null }, proposal: null,
    confirmation_preview: null, intent_session: null, acquisition_session: null,
    deepfetch: { request_ref: requestRef, correlation_ref: "correlation-preparation", basis_revision: 3, basis_hash: hash, scope_hash: hash,
      freshness: "current", status: "queued", activity: "waiting_for_runtime", progress: { completed: 0, total: 5 }, recent_events: [],
      authorization_receipt: accepted("deepfetch-authorization", requestRef), run: null, literature_snapshot: null, failure: null },
    recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } },
    receipts: { human_confirmation: { status: "not_attempted" }, quest_goal: { status: "not_attempted" }, broad_research_authorization: { status: "not_attempted" },
      question_content: { status: "not_attempted" }, question_identity: { status: "not_attempted" }, cycle_activation: { status: "not_attempted" } },
  };
  const preparation = { ref: requestRef, started_at: startedAt, completed_at: null as number | null,
    operation_state: "active" as string | null, retry_permitted: false };
  Object.assign(creation.deepfetch!, { preparation });
  const understandingMessage = "正在阅读已选材料中的研究范围与未完成问题。";
  const appendMessage = "已核对材料引用，继续整理可以检索的证据缺口。";
  const draftingMessage = "正在结合本轮文献整理可验证的第一问。";
  const publicMessage = (id: string, text: string) => JSON.stringify({ type: "item.completed", item: { id, type: "agent_message", phase: "commentary", text } }) + "\n";
  let preparationOutput = publicMessage("understanding-1", understandingMessage);
  const synthesisOutput = publicMessage("drafting-1", draftingMessage);
  const preparationRequests: Array<{ requestRef: string | null; preparationRef: string | null; generationRef: string | null; after: number }> = [];
  const generationRequests: Array<{ generationRef: string | null; after: number }> = [];
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  const outputPage = (ref: string, text: string, after: number, phase: string, status = "running") => {
    const bytes = Buffer.from(text);
    return { generation_ref: ref, status, availability: "ready", phase,
      text: bytes.subarray(after).toString("utf8"), next_offset: bytes.length, source_bytes: bytes.length, has_more: false };
  };
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json({ ...snapshot, quest_creation: { ...snapshot.quest_creation, current: creation } });
    if (url.pathname === "/api/v1/quest-initializations/init-preparation") return json(creation);
    if (url.pathname.endsWith("/preparation-output")) {
      const after = Number(url.searchParams.get("after"));
      preparationRequests.push({ requestRef: url.searchParams.get("request_ref"), preparationRef: url.searchParams.get("preparation_ref"), generationRef: url.searchParams.get("generation_ref"), after });
      return json(outputPage(preparation.ref, preparationOutput, after, "understanding", creation.deepfetch?.status));
    }
    if (url.pathname.endsWith("/proposal-output")) {
      const after = Number(url.searchParams.get("after"));
      generationRequests.push({ generationRef: url.searchParams.get("generation_ref"), after });
      return json(outputPage(generationRef, synthesisOutput, after, "drafting", creation.proposal_generation?.status));
    }
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  const progress = page.getByTestId("first-question-progress");
  const elapsed = progress.locator(":scope > header > span");
  const stages = progress.locator(".first-question-stage-meter > div");
  const activity = progress.getByRole("log", { name: "第一问最近活动" });
  await expect(activity).toBeVisible();
  await expect(activity).toContainText(understandingMessage);
  await expect(progress.locator("h3")).toHaveText("正在理解已有资料");
  await expect(progress).not.toHaveClass(/is-failed/);
  await expect(progress).not.toContainText("本次准备未完成");
  await expect(progress).not.toContainText("本轮耗时");
  await expect(elapsed).toHaveText("已用时 01:05");
  await expect(stages.nth(0)).toHaveAttribute("data-state", "current");
  await expect(stages.nth(1)).toHaveAttribute("data-state", "pending");
  preparationOutput += publicMessage("understanding-2", appendMessage);
  await expect(activity).toContainText(appendMessage);
  expect(preparationRequests.length).toBeGreaterThan(1);
  expect(preparationRequests.every(request => request.requestRef === requestRef && request.generationRef === null)).toBe(true);
  expect(preparationRequests.some(request => request.after > 0)).toBe(true);
  expect(generationRequests).toHaveLength(0);

  const retriedMessage = "重新核对本次材料，旧运行输出不属于此次准备。";
  preparation.ref = requestRef + ":preparation:retry-2";
  preparation.started_at = startedAt;
  preparationOutput = publicMessage("retry-understanding", retriedMessage);
  await expect(activity).toContainText(retriedMessage);
  await expect(activity).not.toContainText(understandingMessage);
  await expect(activity).not.toContainText(appendMessage);
  expect(preparationRequests.find(request => request.preparationRef === preparation.ref)?.after).toBe(0);
  await progress.screenshot({ path: testInfo.outputPath("preparation-retry-current-output.png") });

  creation.deepfetch!.status = "failed";
  creation.deepfetch!.failure = { code: "creation_input_stale" };
  preparation.operation_state = "failed";
  preparation.retry_permitted = true;
  preparation.completed_at = startedAt + 63;
  await expect(progress).toContainText("本次运行已结束，目标、材料和已有草稿仍保留。可以重新生成第一问。");
  await expect(elapsed).toHaveText("本轮耗时 01:03");
  await progress.screenshot({ path: testInfo.outputPath("preparation-confirmed-failed.png") });

  creation.deepfetch!.failure = { code: "protected_creation_unknown_outcome" };
  preparation.ref = requestRef + ":preparation:uncertain-empty";
  preparationOutput = "";
  preparation.operation_state = "unknown_outcome";
  preparation.retry_permitted = false;
  preparation.completed_at = startedAt + 63;
  await expect(progress).toContainText("运行是否结束尚未确认，请等待核对后再重试。");
  await expect(progress).not.toContainText("本次运行已结束");
  await expect(progress).not.toContainText("本次生成已结束");
  await expect(progress.getByTestId("proposal-output")).toContainText("本次资料准备仍需核对，尚无可展示的运行活动。");
  await expect(elapsed).toHaveText("已记录用时 01:03");
  await progress.screenshot({ path: testInfo.outputPath("preparation-unknown-outcome.png") });
  creation.deepfetch!.status = "queued";
  creation.deepfetch!.failure = null;
  preparation.operation_state = "active";
  preparation.completed_at = null;

  creation.creation_basis = { basis_ref: "prepared-material-basis", basis_hash: hash, kind: "prepared", freshness: "current",
    understanding: { material_composition: [], work_already_done: [], claims_and_conditions: [], conflicts: [], gaps: [], unfinished_questions: [] },
    sources: [{ material_key: "research-notes", relative_path: "research-notes.md", selection_reason: null,
      coverage: { kind: "read", unread_description: "" } }], corrections: [], search_assessment: null };
  const deepfetch = creation.deepfetch!;
  deepfetch.status = "running";
  deepfetch.activity = "web_research";
  deepfetch.progress.completed = 1;
  deepfetch.run = { run_ref: "run-preparation", status: "running", attempt_ref: "attempt-search", attempt_generation: 1,
    attempt_started_at: startedAt, attempt_completed_at: null, provider_operation_retry_permitted: true,
    root_session_ref: "root-search", native_session_ref: null, fence_ref: null, runtime_binding_hash: hash, execution_receipt: null, failure: null };
  deepfetch.recent_events = [{ sequence: 1, category: "web_search", status: "running", label: "正在检索与材料缺口相关的论文" }];
  await expect(progress.locator("h3")).toHaveText("正在检索与阅读资料");
  await expect(progress.getByTestId("deepfetch-live-events")).toContainText("正在检索与材料缺口相关的论文");
  await expect(progress.getByTestId("proposal-output")).toHaveCount(0);
  await expect(progress).not.toContainText("正在理解已有资料");
  await expect(stages.nth(0)).toHaveAttribute("data-state", "done");
  await expect(stages.nth(1)).toHaveAttribute("data-state", "current");
  // Ignore the previous failed generation's timestamps from a different draft basis.
  await expect(elapsed).toHaveText("已用时 01:05");
  const settledPreparationCount = preparationRequests.length;
  // Observe the next scheduled output-poll interval: the old producer must be stopped.
  await page.waitForTimeout(2200);
  expect(preparationRequests).toHaveLength(settledPreparationCount);

  deepfetch.status = "succeeded";
  deepfetch.activity = "proposal_drafting";
  deepfetch.run.status = "executed";
  deepfetch.run.attempt_completed_at = startedAt + 8;
  deepfetch.progress.completed = 4;
  deepfetch.recent_events = [{ sequence: 2, category: "web_search", status: "completed", label: "本轮文献检索已完成" }];
  deepfetch.literature_snapshot = { status: "accepted", snapshot_ref: "snapshot-preparation", request_ref: requestRef,
    initialization_id: creation.initialization_id, draft_revision: 3, draft_hash: hash, scope_hash: hash, completion: "complete",
    summary_ref: "summary-preparation", summary_hash: hash, papers_ref: "papers-preparation", papers_hash: hash,
    fulltexts_ref: "fulltexts-preparation", fulltexts_hash: hash, limitations: [], snapshot_hash: hash, paper_count: 2, fulltext_count: 1,
    receipt: accepted("literature-snapshot", "snapshot-preparation") };
  creation.proposal_generation = { ref: generationRef, basis_revision: 3, basis_hash: hash, status: "running", adapter_kind: "production",
    attempt_count: 1, created_at: startedAt + 9, started_at: startedAt + 9, completed_at: null, failure: null, literature_snapshot_ref: "snapshot-preparation" };
  await expect(activity).toContainText(draftingMessage);
  await expect(activity).not.toContainText(understandingMessage);
  await expect(activity).not.toContainText(appendMessage);
  await expect(progress.locator("h3")).toHaveText("正在整理研究依据与起草第一问");
  expect(generationRequests[0]).toEqual({ generationRef, after: 0 });
  expect(generationRequests.every(request => request.generationRef === generationRef)).toBe(true);
  await expect(stages.nth(1)).toHaveAttribute("data-state", "done");
  await expect(stages.nth(2)).toHaveAttribute("data-state", "current");
  await expect(stages.nth(3)).toHaveAttribute("data-state", "pending");
  await expect(progress.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "2");
  // The later generation start must not reset the same preparation card's elapsed time.
  await expect(elapsed).toHaveText("已用时 01:05");
  expect(preparationRequests).toHaveLength(settledPreparationCount);

  await page.clock.setFixedTime(new Date((startedAt + 95) * 1000));
  creation.proposal_generation.status = "failed";
  creation.proposal_generation.completed_at = startedAt + 89;
  creation.proposal_generation.failure = { code: "fixture_drafting_failed" };
  await expect(progress.locator("h3")).toHaveText("本次准备未完成");
  await expect(elapsed).toHaveText("本轮耗时 01:29");
  await page.clock.setFixedTime(new Date((startedAt + 120) * 1000));
  await expect(elapsed).toHaveText("本轮耗时 01:29");
});

import { useEffect, useRef, useState } from "react";
import {
  fetchResearchContent, fetchResearchLibrary, submitResearchInput,
  type ResearchContentPage, type ResearchContentReader, type ResearchLibraryEntry, type ResearchLibraryItem,
  type ResearchLibraryPage,
} from "./api";
import { useOutputLanguage } from "./OutputLanguage";

const entries: ResearchLibraryEntry[] = ["questions", "baselines", "datasets", "literature", "human"];
const labels = {
  zh: ["研究问题", "研究方法", "数据集", "文献", "人类输入"],
  en: ["Questions", "Methods", "Datasets", "Literature", "Human input"],
};
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function itemName(item: ResearchLibraryItem, fallback: string): string {
  return text(item.name) || text(item.title) || text(item.display_name) || text(item.version_label) || fallback;
}
export function ResearchLibrary({ questRef, questionRef }: { questRef: string | null; questionRef: string | null }) {
  const { language } = useOutputLanguage();
  const t = (zh: string, en: string) => language === "zh" ? zh : en;
  const [entry, setEntry] = useState<ResearchLibraryEntry>("questions");
  const [queries, setQueries] = useState<Record<string, string>>({});
  const [queryDraft, setQueryDraft] = useState("");
  const [offsets, setOffsets] = useState<Record<string, number[]>>({});
  const [scopes, setScopes] = useState<Record<string, { name: string; filters: Record<string, string> }[]>>({});
  const [bodyMeta, setBodyMeta] = useState<ResearchContentPage | null>(null);
  const [page, setPage] = useState<ResearchLibraryPage | null>(null);
  const [requestBusy, setRequestBusy] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const requestPage = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<{ name: string; reader: ResearchContentReader } | null>(null);
  const [body, setBody] = useState("");
  const [bodyNext, setBodyNext] = useState<number | null>(null);
  const [bodyBusy, setBodyBusy] = useState(false);
  const [bodyError, setBodyError] = useState<string | null>(null);
  const bodyRequest = useRef<AbortController | null>(null);
  const [guidance, setGuidance] = useState("");
  const [questionScope, setQuestionScope] = useState(false);
  const [inputBusy, setInputBusy] = useState(false);
  const [inputSaved, setInputSaved] = useState(false);
  const [inputError, setInputError] = useState<string | null>(null);
  const history = offsets[entry] ?? [0];
  const offset = history[history.length - 1];
  const pageNext = page?.next_offset ?? page?.items.reduce<number | null>((next, item) => {
    const candidates = [item.next_run_offset, item.next_evaluation_offset].filter((value): value is number => typeof value === "number");
    return candidates.length ? Math.min(next ?? Infinity, ...candidates) : next;
  }, null) ?? null;
  const query = queries[entry] ?? "";
  const scope = scopes[entry] ?? [];
  const filters = scope[scope.length - 1]?.filters;
  function expand(name: string, filters: Record<string, string>) {
    setScopes(value => ({ ...value, [entry]: [...scope, { name, filters }] }));
    setOffsets(value => ({ ...value, [entry]: [0] }));
  }
  useEffect(() => {
    setPage(null); setSelected(null); setBody(""); setBodyMeta(null); bodyRequest.current?.abort(); requestPage.current?.abort(); setRequestBusy(false); setRequestError(null);
    if (!questRef) return;
    const controller = new AbortController(); setBusy(true); setError(null);
    fetchResearchLibrary(entry, questRef, query, offset, controller.signal, filters).then(value => {
      if (!Array.isArray(value.items)) throw new Error("research_library_response_invalid");
      if (!controller.signal.aborted) setPage(value);
    }).catch(caught => {
      if (!controller.signal.aborted) setError(String(caught instanceof Error ? caught.message : caught));
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false);
    });
    return () => controller.abort();
  }, [entry, questRef, query, offset, revision, filters]);
  useEffect(() => () => { bodyRequest.current?.abort(); requestPage.current?.abort(); }, []);
  async function moreRequests() {
    if (!questRef || !page?.request_next_cursor) return;
    requestPage.current?.abort();
    const controller = new AbortController(); requestPage.current = controller;
    setRequestBusy(true); setRequestError(null);
    try {
      const next = await fetchResearchLibrary("human", questRef, query, 0, controller.signal, { request_cursor: page.request_next_cursor });
      if (!controller.signal.aborted) setPage(prior => prior ? { ...prior, items: [...prior.items, ...next.items], request_next_cursor: next.request_next_cursor } : prior);
    } catch (caught) {
      if (!controller.signal.aborted) setRequestError(String(caught instanceof Error ? caught.message : caught));
    } finally { if (!controller.signal.aborted) setRequestBusy(false); }
  }
  async function read(reader: ResearchContentReader, name: string, next = 0) {
    if (!questRef) return;
    bodyRequest.current?.abort();
    const controller = new AbortController(); bodyRequest.current = controller;
    setBodyBusy(true); setBodyError(null);
    if (next === 0) { setSelected({ name, reader }); setBody(""); setBodyNext(null); setBodyMeta(null); }
    try {
      const value = await fetchResearchContent(questRef, reader, next, controller.signal);
      if (controller.signal.aborted) return;
      const part = text(value.text) || text(value.content);
      setBody(prior => next === 0 ? part : prior + part);
      setBodyNext(value.complete === true ? null : value.next_offset ?? null);
      setBodyMeta(prior => value.kind === "directory" && next > 0
        ? { ...value, entries: [...(Array.isArray(prior?.entries) ? prior.entries : []), ...(Array.isArray(value.entries) ? value.entries : [])] }
        : value);
      if (!part && value.kind !== "directory" && value.complete !== true) setBodyError(text(value.status) || "content_unavailable");
    } catch (caught) {
      if (!controller.signal.aborted) setBodyError(String(caught instanceof Error ? caught.message : caught));
    } finally { if (!controller.signal.aborted) setBodyBusy(false); }
  }
  async function saveInput() {
    if (!questRef || !guidance.trim()) return;
    setInputBusy(true); setInputError(null); setInputSaved(false);
    try {
      await submitResearchInput(questRef, questionScope ? questionRef : null, guidance.trim());
      setGuidance(""); setInputSaved(true); setRevision(value => value + 1);
    } catch (caught) { setInputError(String(caught instanceof Error ? caught.message : caught)); }
    finally { setInputBusy(false); }
  }
  return <section className="research-library" aria-label={t("五大研究入口", "Research library")}>
    <div className="library-intro"><h3>{t("找到材料，继续研究", "Find material and continue research")}</h3>
      <p>{t("在当前研究项目内发现已有问题、方法、原文和指导。保存的材料可供后续问题与研究轮次再次使用。", "Discover questions, methods, original material, and guidance in this project. Saved material remains available to later questions and research cycles.")}</p>
      <small>{t("语言设置用于新生成的内容；已保存的原文保持原貌。", "The language setting applies to new content. Saved originals remain unchanged.")}</small></div>
    {!questRef ? <p role="status">{t("先创建或选择研究项目，再浏览项目的研究积累。下方仍可保存资料。", "Create or select a research project to browse its knowledge. You can still save files below.")}</p> : <>
      <div className="library-tabs" role="tablist" aria-label={t("研究入口", "Research entries")}>
        {entries.map((key, index) => <button type="button" role="tab" key={key} aria-selected={entry === key} onClick={() => { setEntry(key); setQueryDraft(queries[key] ?? ""); }}>{labels[language][index]}</button>)}
      </div>
      <form className="library-search" onSubmit={event => { event.preventDefault(); setQueries(value => ({ ...value, [entry]: queryDraft.trim() })); setOffsets(value => ({ ...value, [entry]: [0] })); setScopes(value => ({ ...value, [entry]: [] })); setRevision(value => value + 1); }}>
        <input aria-label={t("查找研究材料", "Find research material")} value={queryDraft} onChange={event => setQueryDraft(event.target.value)} placeholder={t("输入问题、名称或研究线索", "A question, name, or research clue")} />
        <button disabled={busy} type="submit">{t("查找", "Search")}</button>
      </form>
      {scope.length ? <nav className="library-breadcrumb" aria-label={t("材料层级", "Material hierarchy")}><button onClick={() => { setScopes(value => ({ ...value, [entry]: scope.slice(0, -1) })); setOffsets(value => ({ ...value, [entry]: [0] })); }}>{t("返回上一级", "Back")}</button><span>{scope.map(value => value.name).join(" / ")}</span></nav> : null}
      {entry === "human" ? <form className="library-guidance" onSubmit={event => { event.preventDefault(); void saveInput(); }}>
        <h4>{t("补充研究指导", "Add research guidance")}</h4>
        <p>{t("保存你的意见、背景或材料说明，供研究代理查找和采用。一般聊天不会自动存入这里。", "Save your guidance, background, or material notes for research agents to find and use. Ordinary chat is not saved here automatically.")}</p>
        <label>{t("研究指导", "Research guidance")}<textarea rows={3} value={guidance} onChange={event => { setGuidance(event.target.value); setInputSaved(false); }} required /></label>
        {questionRef ? <label className="library-scope"><input type="checkbox" checked={questionScope} onChange={event => setQuestionScope(event.target.checked)} />{t("仅适用于当前问题", "Applies only to the current question")}</label> : null}
        <button disabled={inputBusy || !guidance.trim()}>{inputBusy ? t("正在保存…", "Saving…") : t("保存指导", "Save guidance")}</button>
        {inputSaved ? <p role="status">{t("已保存，后续研究可以发现这条指导。", "Saved. Future research can discover this guidance.")}</p> : null}
        {inputError ? <div role="alert">{t("未能保存。请重试。", "Could not save. Please retry.")}<details><summary>{t("技术详情", "Technical details")}</summary><code>{inputError}</code></details></div> : null}
      </form> : null}
      <div className="library-columns">
        <div className="library-results" aria-busy={busy}>
          {busy ? <p role="status">{t("正在查找材料…", "Finding material…")}</p> : null}
          {error ? <div role="alert"><p>{t("暂时无法读取此入口。请重试。", "This entry could not be loaded. Please retry.")}</p><button onClick={() => setRevision(value => value + 1)}>{t("重试", "Retry")}</button><details><summary>{t("技术详情", "Technical details")}</summary><code>{error}</code></details></div> : null}
          {page && page.items.length === 0 ? <p>{t("没有找到匹配材料。试试其他线索或入口。", "No matching material. Try another clue or entry.")}</p> : null}
          {page?.items.map((item, index) => {
            const name = itemName(item, item.variant_ref ? t("方法变体", "Method variant") : t("研究记录", "Research record"));
            const reader = item.reader;
            const judgments = Array.isArray(item.judgments) ? item.judgments as ResearchLibraryItem[] : [];
            return <article className="library-card" key={text(item.ref) || text(item.question_ref) || text(item.record_ref) || String(index)}>
              <h4>{name}</h4>{text(item.summary) ? <p>{text(item.summary)}</p> : null}
              {entry === "datasets" && item.dataset_ref && !item.dataset_version_ref ? <button onClick={() => expand(name, { dataset_ref: text(item.dataset_ref) })}>{t("查看数据版本", "View data versions")}</button> : null}
              {entry === "baselines" && item.baseline_ref && !item.variant_ref ? <button onClick={() => expand(name, { baseline_ref: text(item.baseline_ref) })}>{t("查看方法变体", "View method variants")}</button> : null}
              {entry === "baselines" && item.variant_ref && !Array.isArray(item.runs) ? <button onClick={() => expand(name, { baseline_ref: text(item.baseline_ref), variant_ref: text(item.variant_ref) })}>{t("查看实施与评价", "View runs and evaluations")}</button> : null}
              {reader?.source_ref ? <button type="button" onClick={() => void read(reader, name)}>{t("阅读原文", "Read original")}</button> : <p className="library-muted">{t("展开详情查看版本与关联。", "Expand details for versions and relationships.")}</p>}
              {item.history_reader && typeof item.history_reader === "object" ? <button onClick={() => void read(item.history_reader as ResearchContentReader, name)}>{t("阅读研究历史", "Read research history")}</button> : null}
              {Array.isArray(item.readers) && item.readers.length > 1 ? <details><summary>{t("此版本的所有材料", "All material in this version")}</summary>{(item.readers as ResearchContentReader[]).map((value, idx) => <button key={idx} onClick={() => void read(value, name)}>{t("读取材料", "Read material")} {idx + 1}</button>)}</details> : null}
              {Array.isArray(item.runs) ? <section className="library-runs"><h5>{t("实际实施", "Runs")}</h5>{(item.runs as ResearchLibraryItem[]).map((run, idx) => <div key={idx}><b>{itemName(run, t("实施", "Run") + " " + (idx + 1))}</b>{text(run.summary) ? <p>{text(run.summary)}</p> : null}{run.reader?.source_ref ? <button onClick={() => void read(run.reader!, name)}>{t("阅读实施记录", "Read run")}</button> : run.reader?.ref ? <button onClick={() => expand(itemName(run, t("实施产物", "Run artifacts")), { formal_ref: run.reader!.ref! })}>{t("查看实施产物", "View run artifacts")}</button> : null}<details><summary>{t("实施与产物详情", "Run and artifact details")}</summary><pre>{JSON.stringify(run, null, 2)}</pre></details></div>)}</section> : null}
              {judgments.length ? <details><summary>{t("不同问题的阅读判断", "Readings for different questions")} ({judgments.length})</summary>{judgments.map((judgment, idx) => <div key={idx}><p>{text(judgment.summary)}</p>{judgment.reader ? <button onClick={() => void read(judgment.reader!, name)}>{t("阅读完整判断", "Read full judgment")}</button> : null}</div>)}</details> : null}
              <details><summary>{t("版本与关联详情", "Version and relationship details")}</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>
            </article>;
          })}
          {entry === "human" && page?.request_next_cursor ? <button disabled={requestBusy || busy} onClick={() => void moreRequests()}>{requestBusy ? t("正在读取回复…", "Reading responses…") : t("更多请求回复", "More request responses")}</button> : null}
          {requestError ? <div role="alert">{t("无法读取更多回复，请重试。", "Could not read more responses. Please retry.")}<details><summary>{t("技术详情", "Technical details")}</summary><code>{requestError}</code></details></div> : null}
          <nav className="library-pagination" aria-label={t("结果分页", "Result pages")}>
            <button disabled={busy || history.length < 2} onClick={() => setOffsets(value => ({ ...value, [entry]: history.slice(0, -1) }))}>{t("上一页", "Previous")}</button>
            <button disabled={busy || pageNext == null} onClick={() => { if (pageNext != null) setOffsets(value => ({ ...value, [entry]: [...history, pageNext] })); }}>{t("下一页", "Next")}</button>
          </nav>
        </div>
        <section className="library-original" aria-live="polite">
          <h4>{selected?.name ?? t("精确原文", "Exact original")}</h4>
          {!selected ? <p className="library-muted">{t("选择一份材料，阅读保留的原始内容。", "Choose material to read its preserved content.")}</p> : <>
            {bodyMeta?.kind === "directory" ? <div><p>{t("这是一个资料目录，选择文件读取原文。", "This material is a directory. Choose a file to read.")}</p>{(Array.isArray(bodyMeta.entries) ? bodyMeta.entries as { path: string; size?: number }[] : []).map(value => <p key={value.path}><button onClick={() => void read({ ...selected.reader, entry_path: value.path }, value.path)}>{value.path}</button></p>)}</div> : null}
            {bodyMeta?.encoding === "base64" ? <p>{t("这是二进制材料，可下载精确版本查看。", "This is binary material. Download the exact version to inspect it.")}<br /><a href={`/api/v1/research-assets/${encodeURIComponent(selected.reader.version_ref ?? "")}/content`}>{t("下载原件", "Download original")}</a></p> : body ? <ReadableOriginal body={body} language={language} /> : null}
            {!body && bodyMeta?.complete === true && bodyMeta?.kind !== "directory" ? <p>{t("此原文为空。", "This original is empty.")}</p> : null}
            {bodyBusy ? <p role="status">{t("正在读取…", "Reading…")}</p> : null}
            {bodyError ? <div role="alert"><p>{t("原文暂时不可读。已读内容保留，可重试。", "The original is temporarily unavailable. Previously read content is retained.")}</p><button onClick={() => void read(selected.reader, selected.name, bodyNext ?? 0)}>{t("重试读取", "Retry reading")}</button><details><summary>{t("技术详情", "Technical details")}</summary><code>{bodyError}</code></details></div> : null}
            {bodyNext != null ? <button disabled={bodyBusy} onClick={() => void read(selected.reader, selected.name, bodyNext)}>{t("继续读取", "Read more")}</button> : null}
            <details><summary>{t("精确引用", "Exact reference")}</summary><pre>{JSON.stringify(selected.reader, null, 2)}</pre></details>
          </>}
        </section>
      </div>
    </>}
  </section>;
}


function ReadableOriginal({ body, language }: { body: string; language: "zh" | "en" }) {
  let parsed: unknown;
  try { parsed = JSON.parse(body); } catch { return <pre className="library-prose">{body}</pre>; }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return <pre className="library-prose">{body}</pre>;
  const record = parsed as Record<string, unknown>;
  const fields: [string, string, string][] = [
    ["title","标题","Title"],["name","名称","Name"],["text","原文","Original"],["summary","摘要","Summary"],
    ["note","说明","Note"],["body","正文","Body"],["markdown","正文","Body"],
    ["unknown_statement","研究问题","Research question"],["answer_shape","预期回答","Expected answer"],
    ["applicability_scope","适用范围","Scope"],["background_context","背景","Background"],
    ["requirements_constraints","要求与约束","Requirements and constraints"],
  ];
  const visible = fields.filter(([key]) => typeof record[key] === "string");
  return <div>{visible.map(([key, zh, en]) => <section key={key}><h5>{language === "zh" ? zh : en}</h5><pre className="library-prose">{String(record[key])}</pre></section>)}
    {!visible.length ? <p>{language === "zh" ? "此来源保留了结构化研究记录，展开完整原文查看。" : "This source preserves a structured research record. Expand the complete original to inspect it."}</p> : null}
    <details><summary>{language === "zh" ? "完整原文与技术字段" : "Complete original and technical fields"}</summary><pre>{JSON.stringify(record, null, 2)}</pre></details>
  </div>;
}

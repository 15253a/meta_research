import { useEffect, useMemo, useRef, useState } from "react";
import { fetchQuestRuntimeConditions, fetchQuestRuntimeDevices, ProductError, saveQuestRuntimeConditions, type QuestRuntimeConditions, type ResearchStyle, type LiteratureConfiguration } from "./api";
import { RESEARCH_STYLES, researchStyleLabel } from "./researchStyle";
import { ExternalMcpSettings } from "./ExternalMcpSettings";
import { SearchSourcesSettings } from "./SearchSourcesSettings";
import { MetaTrace } from "./MetaTrace";
import { TimeBudgetField } from "./TimeBudgetField";
import { validTimeBudget } from "./timeBudget";
import {
  isConditionsObject, mergeRuntimeConditionDevices, parseRuntimeConditions, replaceRuntimeConditionsJson,
  runtimeConditionDeviceIds, runtimeConditionDevices, updateRuntimeConditionDevices,
  type RuntimeConditionDevice, type RuntimeConditionsObject,
} from "./runtime-conditions-form";
import "./runtime-conditions.css";

const LITERATURE_MODES = [["oa_then_institution", "公开全文与可选图书馆"], ["oa_only", "只搜索开放获取资源"], ["provided_only", "只使用我提供的材料"]];
const MAX_CONDITIONS_LENGTH = 24000;
// Unsaved edits belong to this page and Quest, never to another Quest or a server receipt.
const conditionDrafts = new Map<string, { basis: QuestRuntimeConditions; text: string; researchStyle: ResearchStyle; library: LiteratureConfiguration | undefined; advanced: boolean; scrollTop: number }>();

export function RuntimeConditions({ questRef, questionRef = null, disabled = false }: {
  questRef: string | null; questionRef?: string | null; disabled?: boolean;
}) {
  return <RuntimeConditionsEntry key={questRef ?? "pre-quest"} questRef={questRef} questionRef={questionRef} disabled={disabled} />;
}

function RuntimeConditionsEntry({ questRef, questionRef, disabled }: { questRef: string | null; questionRef: string | null; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    window.requestAnimationFrame(() => opener.current?.focus({ preventScroll: true }));
  };
  return <>
    <button ref={opener} type="button" className="runtime-conditions-entry" aria-haspopup="dialog" disabled={disabled}
      onClick={() => setOpen(true)}>修改配置</button>
    {open ? <RuntimeConditionsDialog questRef={questRef} questionRef={questionRef} onClose={close} /> : null}
  </>;
}

function RuntimeConditionsDialog({ questRef, questionRef, onClose }: { questRef: string | null; questionRef: string | null; onClose: () => void }) {
  const restored = questRef ? conditionDrafts.get(questRef) : undefined;
  const dialog = useRef<HTMLDialogElement>(null);
  const alive = useRef(false);
  const [basis, setBasis] = useState<QuestRuntimeConditions | null>(null);
  const [text, setText] = useState(restored?.text ?? "");
  const [researchStyle, setResearchStyle] = useState<ResearchStyle>(restored?.researchStyle ?? "balanced");
  const [library, setLibrary] = useState<LiteratureConfiguration | undefined>(restored?.library);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [advanced, setAdvanced] = useState(restored?.advanced ?? false);
  const [devices, setDevices] = useState<RuntimeConditionDevice[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(false);
  const [devicesError, setDevicesError] = useState<string | null>(null);
  const parsed = useMemo(() => parseRuntimeConditions(text), [text]);
  const data = parsed.kind === "structured" ? parsed.data : {};
  const literature = isConditionsObject(data.literature) ? data.literature : {};
  const literatureEditable = data.literature === undefined || isConditionsObject(data.literature);
  const selectedIds = runtimeConditionDeviceIds(data);
  const selectedDevices = runtimeConditionDevices(data) ?? [];
  const choices = mergeRuntimeConditionDevices(devices, selectedDevices);
  for (const uuid of selectedIds ?? []) {
    if (!choices.some(device => device.uuid === uuid)) choices.push({ uuid });
  }
  const controlsDisabled = loading || !basis || saving || parsed.kind !== "structured";
  const invalidBudget = parsed.kind === "structured" && typeof data.time_budget === "string" && !validTimeBudget(data.time_budget);
  const stringField = (value: unknown) => value === undefined || typeof value === "string";
  const unusualFields = !stringField(data.time_budget) || !literatureEditable
    || !stringField(literature.mode) || !stringField(literature.scope_exclusions) || selectedIds === null;

  const rememberDevices = (value: string) => {
    const document = parseRuntimeConditions(value);
    if (document.kind !== "structured") return;
    const selected = runtimeConditionDevices(document.data);
    if (selected) setDevices(previous => mergeRuntimeConditionDevices(previous, selected.map(({ uuid, name, memory_total_mib }) => ({ uuid, name, memory_total_mib }))));
  };
  const editText = (value: string) => {
    setText(value);
    setSaved(false);
    rememberDevices(value);
    const document = parseRuntimeConditions(value);
    const nextLiterature = document.kind === "structured" && isConditionsObject(document.data.literature) ? document.data.literature : null;
    const mode = nextLiterature?.mode;
    if (mode === "oa_only" || mode === "oa_then_institution" || mode === "provided_only") {
      setLibrary(previous => previous ? { ...previous, mode, institution_required: mode === "oa_then_institution" && previous.institution_required } : previous);
    }
  };
  const editFields = (value: RuntimeConditionsObject) => {
    if (controlsDisabled || parsed.kind !== "structured") return;
    const next = replaceRuntimeConditionsJson(parsed, value);
    if (next.length > MAX_CONDITIONS_LENGTH) {
      setError("修改后超过 24,000 字，请在高级编辑中缩短内容后再试。");
      return;
    }
    editText(next);
  };

  useEffect(() => {
    alive.current = true;
    const element = dialog.current;
    element?.showModal();
    if (element && restored) element.scrollTop = restored.scrollTop;
    return () => { alive.current = false; element?.close(); };
  }, []);

  useEffect(() => {
    if (!questRef) { setLoading(false); return; }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    let current = true;
    setLoading(true);
    setError(null);
    void fetchQuestRuntimeConditions(questRef, controller.signal).then(value => {
      if (!current) return;
      if (value.quest_ref !== questRef || typeof value.text !== "string" || typeof value.revision !== "string") {
        throw new Error("runtime_conditions_identity_invalid");
      }
      const cached = attempt === 0 ? conditionDrafts.get(questRef) : undefined;
      const dirty = cached && (cached.text !== cached.basis.text || cached.researchStyle !== (cached.basis.research_style ?? "balanced") || JSON.stringify(cached.library) !== JSON.stringify(cached.basis.literature_configuration));
      const stale = Boolean(dirty && cached.basis.revision !== value.revision);
      setBasis(dirty ? cached.basis : value);
      setText(dirty ? cached.text : value.text);
      setResearchStyle(dirty ? cached.researchStyle : value.research_style ?? "balanced");
      setLibrary(dirty ? cached.library : value.literature_configuration);
      setConflict(stale);
      if (stale) setError("运行条件已变化。当前编辑已保留，请核对最新版本后再继续。");
      rememberDevices(value.text);
      setAdvanced(cached?.advanced ?? parseRuntimeConditions(value.text).kind !== "structured");
    }).catch(() => {
      if (current) setError("运行条件读取失败，请重试。");
    }).finally(() => {
      window.clearTimeout(timeout);
      if (current) setLoading(false);
    });
    return () => { current = false; controller.abort(); window.clearTimeout(timeout); };
  }, [questRef, attempt]);

  useEffect(() => {
    if (questRef && basis && !loading) conditionDrafts.set(questRef, { basis, text, researchStyle, library, advanced, scrollTop: dialog.current?.scrollTop ?? 0 });
  }, [questRef, basis, text, researchStyle, library, advanced, loading]);

  useEffect(() => {
    if (!questRef) return;
    if (!questionRef) {
      setDevicesLoading(false);
      setDevicesError("暂时无法读取完整设备清单，当前已选设备仍保留。请稍后重新打开。");
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    let current = true;
    setDevicesLoading(true);
    setDevicesError(null);
    void fetchQuestRuntimeDevices(questRef, questionRef, controller.signal).then(value => {
      if (current) setDevices(previous => mergeRuntimeConditionDevices(previous, value));
    }).catch(() => {
      if (current) setDevicesError("完整设备清单读取失败，当前已选设备仍保留。请稍后重新打开。");
    }).finally(() => {
      window.clearTimeout(timeout);
      if (current) setDevicesLoading(false);
    });
    return () => { current = false; controller.abort(); window.clearTimeout(timeout); };
  }, [questRef, questionRef, attempt]);

  const save = async () => {
    if (!questRef || !basis || saving || conflict || invalidBudget || !text.trim() || text.length > MAX_CONDITIONS_LENGTH) return;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const value = await saveQuestRuntimeConditions(questRef, text, basis.revision, researchStyle, library);
      if (!alive.current) return;
      if (value.quest_ref !== questRef || typeof value.text !== "string" || typeof value.revision !== "string") {
        throw new Error("runtime_conditions_identity_invalid");
      }
      setBasis(value);
      setText(value.text);
      setResearchStyle(value.research_style ?? "balanced");
      setLibrary(value.literature_configuration);
      rememberDevices(value.text);
      setSaved(true);
    } catch (caught) {
      if (!alive.current) return;
      const stale = caught instanceof ProductError && caught.code === "runtime_conditions_stale";
      setConflict(stale);
      setError(stale ? "运行条件已变化。当前编辑已保留，请核对最新版本后再继续。" : "保存失败，当前编辑已保留，请重试。");
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  return <dialog ref={dialog} className="runtime-conditions-dialog workspace-modal" aria-labelledby="runtime-conditions-title"
    onScroll={event => { if (questRef && basis) conditionDrafts.set(questRef, { basis, text, researchStyle, library, advanced, scrollTop: event.currentTarget.scrollTop }); }}
    onCancel={event => { event.preventDefault(); onClose(); }}>
    {questRef ? <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <header><MetaTrace variant="brief" /><div><h2 id="runtime-conditions-title">修改配置</h2><p>当前 Quest · {questRef}</p></div><button type="button" aria-label="关闭修改配置" onClick={onClose}>×</button></header>
      <p id="runtime-conditions-help">保存后用于后续新调用，不改变正在进行的调用或已保存的研究成果。</p>
      <p>关闭后保留本页面未保存的编辑；刷新后按已保存配置恢复。</p>
      {loading ? <p role="status">正在读取运行条件…</p> : null}
      <div className="runtime-conditions-fields">
        <label><span>研究风格</span><select aria-label="研究风格" aria-describedby="runtime-research-style-help"
          value={researchStyle} disabled={loading || !basis || saving}
          onChange={event => { setResearchStyle(event.currentTarget.value as ResearchStyle); setSaved(false); }}>
          {RESEARCH_STYLES.map(style => <option key={style.value} value={style.value}>{style.label}</option>)}
        </select></label>
        {basis ? <p>当前研究风格：{researchStyleLabel(basis.research_style ?? "balanced")}</p> : null}
        <p id="runtime-research-style-help" className="runtime-conditions-exclusions">
          {RESEARCH_STYLES.find(style => style.value === researchStyle)?.description} 这是持续研究倾向，仍须遵循你明确保留的条件。
        </p>
        <TimeBudgetField value={typeof data.time_budget === "string" ? data.time_budget : "open"}
          disabled={controlsDisabled || !stringField(data.time_budget)}
          onChange={time_budget => editFields({ ...data, time_budget })} />
      </div>
      <section className="runtime-deepfetch" aria-label="DeepFetch 配置"><h3>DeepFetch 配置</h3>
        <SearchSourcesSettings scope={{ kind: "quest", questRef }} />
        <section className="runtime-fulltext" aria-label="图书馆与全文获取"><h4>图书馆与全文获取</h4>
        <p>图书馆用于获取已发现文献的全文，不参与搜索源勾选。没有图书馆时沿允许范围内的公开网页、PDF 与 OA 路线获取；仅摘要和获取失败会按实际结果报告。</p>
        <div className="runtime-conditions-fields">
        <label><span>文献搜索范围</span><select aria-label="文献搜索范围" value={library?.mode ?? (typeof literature.mode === "string" ? literature.mode : "")}
          disabled={loading || !basis || saving || (!library && (controlsDisabled || !literatureEditable || !stringField(literature.mode)))}
          onChange={event => {
            const mode = event.currentTarget.value as LiteratureConfiguration["mode"];
            if (library) setLibrary({ ...library, mode, institution_required: mode === "oa_then_institution" && library.institution_required });
            if (parsed.kind === "structured" && literatureEditable) editFields({ ...data, literature: { ...literature, mode } });
            setSaved(false);
          }}>
          <ConditionOptions value={library?.mode ?? literature.mode} options={LITERATURE_MODES} />
        </select></label>
        <label><span>图书馆／数据库入口链接</span><input aria-label="图书馆／数据库入口链接" type="url"
          value={library?.library_entry_url ?? ""} disabled={loading || !basis || saving || !library}
          onChange={event => { if (library) setLibrary({ ...library, library_entry_url: event.currentTarget.value }); setSaved(false); }} /></label>
        {library ? <label className="runtime-library-required"><input type="checkbox" aria-label="必须使用机构访问"
          checked={library?.institution_required ?? false} disabled={loading || !basis || saving || library?.mode !== "oa_then_institution"}
          onChange={event => { if (library) setLibrary({ ...library, institution_required: event.currentTarget.checked }); setSaved(false); }} />必须使用机构访问</label> : <p>当前读取未提供独立全文配置，不能从运行条件原文推断连接。请重新读取或在初始化草稿中核对。</p>}
        <label className="runtime-conditions-exclusions"><span>文献排除范围</span><textarea aria-label="文献排除范围" rows={2}
          maxLength={MAX_CONDITIONS_LENGTH} value={typeof literature.scope_exclusions === "string" ? literature.scope_exclusions : ""}
          placeholder="例如：排除的主题、来源或年代；没有可留空"
          disabled={controlsDisabled || !literatureEditable || !stringField(literature.scope_exclusions)}
          onChange={event => editFields({ ...data, literature: { ...literature, scope_exclusions: event.currentTarget.value } })} /></label>
      </div></section></section>
      <fieldset className="runtime-conditions-devices" disabled={controlsDisabled || selectedIds === null}>
        <legend>本机计算卡</legend>
        <p>可选择这项研究初始化时检测到的 GPU。</p>
        {devicesLoading ? <p role="status">正在读取设备清单…</p> : null}
        {devicesError ? <p className="runtime-conditions-note">{devicesError}</p> : null}
        {choices.length ? <div className="runtime-conditions-device-list">{choices.map(device => <label key={device.uuid} className="runtime-conditions-device">
          <input type="checkbox" checked={selectedIds?.includes(device.uuid) ?? false}
            aria-label={`${device.name || "GPU"} · ${device.uuid}`}
            onChange={event => {
              if (!selectedIds) return;
              const next = event.currentTarget.checked ? [...selectedIds, device.uuid] : selectedIds.filter(uuid => uuid !== device.uuid);
              editFields(updateRuntimeConditionDevices(data, next, choices));
            }} />
          <span><strong>{device.name || "GPU"}</strong><small>
            {device.memory_total_mib === undefined ? "" : `${Number((device.memory_total_mib / 1024).toFixed(1))} GiB · `}{device.uuid}
          </small></span>
        </label>)}</div> : !devicesLoading ? <p>暂无可选设备。</p> : null}
        {selectedIds?.length === 0 && !controlsDisabled ? <p>未选择 GPU，将按没有已选 GPU 的条件安排后续工作。</p> : null}
      </fieldset>
      {!loading && basis && parsed.kind !== "structured" ? <p className="runtime-conditions-note">
        {parsed.kind === "invalid" ? "原文中的 JSON 暂时无法解析，选择项已暂停同步。请在高级编辑中修正；原文不会被覆盖。" : "当前运行条件是自由文本，请在高级编辑中修改；选择项不会覆盖原文。"}
      </p> : null}
      {!loading && parsed.kind === "structured" && unusualFields ? <p className="runtime-conditions-note">部分配置使用了特殊格式，请在高级编辑中修改对应字段。</p> : null}
      <details className="runtime-conditions-advanced" open={advanced} onToggle={event => setAdvanced(event.currentTarget.open)}>
        <summary>高级：JSON / 原文编辑</summary>
        <label><span>完整运行条件</span><textarea aria-label="运行条件内容" aria-describedby="runtime-conditions-help"
          rows={12} maxLength={MAX_CONDITIONS_LENGTH} value={text} disabled={loading || !basis || saving}
          onChange={event => editText(event.currentTarget.value)} /></label>
      </details>
      {text.length > MAX_CONDITIONS_LENGTH ? <p role="alert" className="runtime-conditions-error">运行条件最多 24,000 字，请缩短后再保存。</p> : null}
      {error ? <p role="alert" className="runtime-conditions-error">{error}</p> : null}
      {conflict ? <><p>你的编辑仍保留。重新读取会用最新已保存版本替换当前运行条件编辑。</p><button type="button" onClick={() => { setConflict(false); setAttempt(value => value + 1); }}>读取最新运行条件并替换当前编辑</button></> : null}
      {saved ? <p role="status" className="runtime-conditions-saved">已保存，将用于后续新调用。</p> : null}
      <footer><button type="button" onClick={onClose}>{saved || saving ? "关闭" : "取消"}</button>
        {!basis && !loading ? <button type="button" onClick={() => setAttempt(value => value + 1)}>重新读取</button> : null}
        <button type="submit" className="runtime-conditions-save" disabled={!basis || loading || saving || conflict || invalidBudget || !text.trim() || text.length > MAX_CONDITIONS_LENGTH || (text === basis.text && researchStyle === (basis.research_style ?? "balanced") && JSON.stringify(library) === JSON.stringify(basis.literature_configuration))}>
          {saving ? "正在保存…" : "保存运行条件"}</button></footer>
    </form> : <><header className="runtime-conditions-pre-quest"><MetaTrace variant="brief" /><h2 id="runtime-conditions-title">修改配置</h2><button type="button" aria-label="关闭修改配置" onClick={onClose}>×</button></header><p className="runtime-pre-quest-note">尚无当前 Quest。可管理共享搜索源和系统通用 MCP；图书馆与全文获取在新建研究任务的初始化草稿中配置。</p><section className="runtime-deepfetch" aria-label="DeepFetch 配置"><h3>DeepFetch 配置</h3><SearchSourcesSettings scope={{ kind: "shared-only" }} /></section></>}
    <section className="runtime-system" aria-label="系统配置"><h3>系统配置</h3><p>通用 MCP 服务于整个系统的研究 Agent，按已选根类型授权；视频理解等通用能力无需登记为搜索源。</p>
    <ExternalMcpSettings />
    </section>
  </dialog>;
}

function ConditionOptions({ value, options }: { value: unknown; options: string[][] }) {
  const current = typeof value === "string" ? value : "";
  return <>
    {!current ? <option value="">未设置</option> : !options.some(([key]) => key === current) ? <option value={current}>当前值：{current}</option> : null}
    {options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
  </>;
}

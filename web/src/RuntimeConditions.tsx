import { useEffect, useMemo, useRef, useState } from "react";
import { fetchQuestRuntimeConditions, fetchQuestRuntimeDevices, ProductError, saveQuestRuntimeConditions, type QuestRuntimeConditions } from "./api";
import {
  isConditionsObject, mergeRuntimeConditionDevices, parseRuntimeConditions, replaceRuntimeConditionsJson,
  runtimeConditionDeviceIds, runtimeConditionDevices, updateRuntimeConditionDevices,
  type RuntimeConditionDevice, type RuntimeConditionsObject,
} from "./runtime-conditions-form";
import "./runtime-conditions.css";

const TIME_BUDGETS = [["7d", "7 天"], ["30d", "30 天"], ["90d", "90 天"], ["open", "不设硬截止"]];
const LITERATURE_MODES = [["oa_then_institution", "全面搜索（包括图书馆）"], ["oa_only", "只搜索开放获取资源"], ["provided_only", "只使用我提供的材料"]];
const MAX_CONDITIONS_LENGTH = 24000;

export function RuntimeConditions({ questRef, questionRef = null, disabled = false }: {
  questRef: string | null; questionRef?: string | null; disabled?: boolean;
}) {
  return questRef ? <RuntimeConditionsEntry key={questRef} questRef={questRef} questionRef={questionRef} disabled={disabled} /> : null;
}

function RuntimeConditionsEntry({ questRef, questionRef, disabled }: { questRef: string; questionRef: string | null; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    window.requestAnimationFrame(() => opener.current?.focus({ preventScroll: true }));
  };
  return <>
    <button ref={opener} type="button" className="runtime-conditions-entry" aria-haspopup="dialog" disabled={disabled}
      onClick={() => setOpen(true)}>运行条件</button>
    {open ? <RuntimeConditionsDialog questRef={questRef} questionRef={questionRef} onClose={close} /> : null}
  </>;
}

function RuntimeConditionsDialog({ questRef, questionRef, onClose }: { questRef: string; questionRef: string | null; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const alive = useRef(false);
  const [basis, setBasis] = useState<QuestRuntimeConditions | null>(null);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [advanced, setAdvanced] = useState(false);
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
    return () => { alive.current = false; element?.close(); };
  }, []);

  useEffect(() => {
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
      setBasis(value);
      setText(value.text);
      rememberDevices(value.text);
      setAdvanced(parseRuntimeConditions(value.text).kind !== "structured");
    }).catch(() => {
      if (current) setError("运行条件读取失败，请重试。");
    }).finally(() => {
      window.clearTimeout(timeout);
      if (current) setLoading(false);
    });
    return () => { current = false; controller.abort(); window.clearTimeout(timeout); };
  }, [questRef, attempt]);

  useEffect(() => {
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
    if (!basis || saving || conflict || !text.trim() || text.length > MAX_CONDITIONS_LENGTH) return;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const value = await saveQuestRuntimeConditions(questRef, text, basis.revision);
      if (!alive.current) return;
      if (value.quest_ref !== questRef || typeof value.text !== "string" || typeof value.revision !== "string") {
        throw new Error("runtime_conditions_identity_invalid");
      }
      setBasis(value);
      setText(value.text);
      rememberDevices(value.text);
      setSaved(true);
    } catch (caught) {
      if (!alive.current) return;
      const stale = caught instanceof ProductError && caught.code === "runtime_conditions_stale";
      setConflict(stale);
      setError(stale ? "运行条件已变化。当前编辑已保留，请关闭后重新打开，核对最新内容再保存。" : "保存失败，当前编辑已保留，请重试。");
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  return <dialog ref={dialog} className="runtime-conditions-dialog" aria-labelledby="runtime-conditions-title"
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <header><h2 id="runtime-conditions-title">运行条件</h2><button type="button" aria-label="关闭运行条件" onClick={onClose}>×</button></header>
      <p id="runtime-conditions-help">保存后用于后续新调用，不改变正在进行的调用或已保存的研究成果。</p>
      {loading ? <p role="status">正在读取运行条件…</p> : null}
      <div className="runtime-conditions-fields">
        <label><span>时间预算</span><select aria-label="时间预算" value={typeof data.time_budget === "string" ? data.time_budget : ""}
          disabled={controlsDisabled || !stringField(data.time_budget)}
          onChange={event => editFields({ ...data, time_budget: event.currentTarget.value })}>
          <ConditionOptions value={data.time_budget} options={TIME_BUDGETS} />
        </select></label>
        <label><span>文献搜索范围</span><select aria-label="文献搜索范围" value={typeof literature.mode === "string" ? literature.mode : ""}
          disabled={controlsDisabled || !literatureEditable || !stringField(literature.mode)}
          onChange={event => editFields({ ...data, literature: { ...literature, mode: event.currentTarget.value } })}>
          <ConditionOptions value={literature.mode} options={LITERATURE_MODES} />
        </select></label>
        <label className="runtime-conditions-exclusions"><span>文献排除范围</span><textarea aria-label="文献排除范围" rows={2}
          maxLength={MAX_CONDITIONS_LENGTH} value={typeof literature.scope_exclusions === "string" ? literature.scope_exclusions : ""}
          placeholder="例如：排除的主题、来源或年代；没有可留空"
          disabled={controlsDisabled || !literatureEditable || !stringField(literature.scope_exclusions)}
          onChange={event => editFields({ ...data, literature: { ...literature, scope_exclusions: event.currentTarget.value } })} /></label>
      </div>
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
      {saved ? <p role="status" className="runtime-conditions-saved">已保存，将用于后续新调用。</p> : null}
      <footer><button type="button" onClick={onClose}>{saved || saving ? "关闭" : "取消"}</button>
        {!basis && !loading ? <button type="button" onClick={() => setAttempt(value => value + 1)}>重新读取</button> : null}
        <button type="submit" className="runtime-conditions-save" disabled={!basis || loading || saving || conflict || !text.trim() || text.length > MAX_CONDITIONS_LENGTH || text === basis.text}>
          {saving ? "正在保存…" : "保存运行条件"}</button></footer>
    </form>
  </dialog>;
}

function ConditionOptions({ value, options }: { value: unknown; options: string[][] }) {
  const current = typeof value === "string" ? value : "";
  return <>
    {!current ? <option value="">未设置</option> : !options.some(([key]) => key === current) ? <option value={current}>当前值：{current}</option> : null}
    {options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
  </>;
}

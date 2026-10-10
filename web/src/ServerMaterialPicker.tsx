import { useEffect, useId, useRef, useState } from "react";
import {
  browseServerMaterials,
  cancelServerMaterialCursor,
  inspectServerMaterial,
  WorkMaterialError,
  workMaterialErrorMessage,
  type ServerMaterialPage,
  type ServerMaterialSelection,
} from "./workMaterialApi";
import "./server-material-picker.css";

export type ServerMaterialPickerProps = {
  value: ServerMaterialSelection | null;
  onSelect: (selection: ServerMaterialSelection | null) => void;
  selectKind?: "file" | "directory" | "both";
  disabled?: boolean;
  initialPath?: string;
};

type BrowserState =
  | { phase: "idle"; page: null; error: null }
  | { phase: "loading"; page: ServerMaterialPage | null; error: null }
  | { phase: "ready"; page: ServerMaterialPage; error: string | null }
  | { phase: "failed"; page: null; error: string };

function parentPath(path: string): string {
  const at = path.lastIndexOf("/");
  return at <= 0 ? "/" : path.slice(0, at);
}

export function ServerMaterialPicker({ value, onSelect, selectKind = "both", disabled = false, initialPath = "/" }: ServerMaterialPickerProps) {
  const id = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const pathInput = useRef<HTMLInputElement>(null);
  const initialFocusPending = useRef(false);
  const generation = useRef(0);
  const cursor = useRef<string | null>(null);
  const [open, setOpen] = useState(false);
  const [browser, setBrowser] = useState<BrowserState>({ phase: "idle", page: null, error: null });
  const [path, setPath] = useState(initialPath);
  const [candidate, setCandidate] = useState("");
  const [description, setDescription] = useState("");
  const [inspection, setInspection] = useState<"idle" | "loading">("idle");
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [cleanupError, setCleanupError] = useState<string | null>(null);

  async function releaseCursor() {
    const held = cursor.current;
    cursor.current = null;
    if (held) {
      try { await cancelServerMaterialCursor(held); }
      catch (error) { setCleanupError(workMaterialErrorMessage(error)); }
    }
  }

  useEffect(() => () => {
    generation.current += 1;
    if (cursor.current) void cancelServerMaterialCursor(cursor.current).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (open) {
      dialog.current?.showModal();
    } else if (dialog.current?.open) {
      dialog.current.close();
      trigger.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (open && initialFocusPending.current && (browser.phase === "ready" || browser.phase === "failed")) {
      initialFocusPending.current = false;
      pathInput.current?.focus();
    }
  }, [open, browser.phase]);

  function close() {
    initialFocusPending.current = false;
    generation.current += 1;
    setOpen(false);
    setInspection("idle");
    void releaseCursor();
  }

  useEffect(() => {
    if (disabled && open) close();
  }, [disabled]);

  async function browse(nextPath: string, more = false) {
    if (browser.phase === "loading" && more) return;
    const heldPage = more ? browser.page : null;
    const heldCursor = more ? cursor.current : null;
    if (more && !heldCursor) return;
    const requestGeneration = ++generation.current;
    if (!more) await releaseCursor();
    if (requestGeneration !== generation.current) return;
    setPath(nextPath);
    setBrowser({ phase: "loading", page: heldPage, error: null });
    try {
      const page = await browseServerMaterials(nextPath, heldCursor ?? undefined);
      if (requestGeneration !== generation.current) {
        if (page.next_cursor) await cancelServerMaterialCursor(page.next_cursor);
        return;
      }
      cursor.current = page.next_cursor;
      setBrowser({ phase: "ready", page: heldPage ? { ...page, entries: [...heldPage.entries, ...page.entries] } : page, error: null });
    } catch (error) {
      if (requestGeneration !== generation.current) return;
      cursor.current = null;
      const message = workMaterialErrorMessage(error);
      setBrowser(heldPage ? { phase: "ready", page: { ...heldPage, next_cursor: null }, error: message } : { phase: "failed", page: null, error: message });
    }
  }

  function begin() {
    const startingPath = value?.kind === "directory" ? value.absolute_path : value ? parentPath(value.absolute_path) : initialPath;
    setCandidate(value?.absolute_path ?? "");
    setDescription(value?.description ?? "");
    setSelectionError(null);
    setCleanupError(null);
    setInspection("idle");
    setBrowser({ phase: "loading", page: null, error: null });
    initialFocusPending.current = true;
    setOpen(true);
    void browse(startingPath);
  }

  async function choose(selectedPath: string) {
    const requestGeneration = generation.current;
    setInspection("loading");
    setSelectionError(null);
    try {
      const selected = await inspectServerMaterial(selectedPath, description);
      if (requestGeneration !== generation.current) return;
      if (selectKind !== "both" && selected.kind !== selectKind) throw new WorkMaterialError("material_kind_mismatch");
      onSelect(selected);
      close();
    } catch (error) {
      if (requestGeneration === generation.current) setSelectionError(workMaterialErrorMessage(error));
    } finally {
      if (requestGeneration === generation.current) setInspection("idle");
    }
  }

  const busy = browser.phase === "loading" || inspection === "loading";
  const page = browser.page;
  const permitted = (kind: string) => selectKind === "both" ? kind === "file" || kind === "directory" : kind === selectKind;

  return <div className="server-material-picker">
    <div className="server-material-picker__actions">
      <button ref={trigger} type="button" onClick={begin} disabled={disabled}>选择服务器{selectKind === "file" ? "文件" : selectKind === "directory" ? "目录" : "文件或目录"}</button>
      {value && <button type="button" onClick={() => onSelect(null)} disabled={disabled}>清除候选</button>}
    </div>
    {value && <div className="server-material-picker__candidate">
      <strong>{value.kind === "file" ? "文件" : "目录"}候选</strong>
      <code>{value.absolute_path}</code>
      <span>{value.server.hostname} · {value.server.platform} · 运行账户 {value.server.permission_context}</span>
      {value.description && <p>{value.description}</p>}
      <small>已检查来源元数据。尚未提交或读取内容。</small>
    </div>}
    {cleanupError && <p role="status" className="server-material-picker__error">目录浏览清理失败。{cleanupError}</p>}
    <dialog ref={dialog} className="server-material-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-notice`} onKeyDown={event => event.stopPropagation()} onCancel={event => { event.preventDefault(); event.stopPropagation(); close(); }}>
      <div className="server-material-dialog__heading">
        <h3 id={`${id}-title`}>选择服务器上的原始材料</h3>
        <button type="button" onClick={close} aria-label="取消材料选择">关闭</button>
      </div>
      <p id={`${id}-notice`}>选择仅检查来源元数据。使用表单的提交按钮后才会保存材料。</p>
      {page && <dl className="server-material-dialog__server">
        <div><dt>服务器</dt><dd>{page.server.hostname}</dd></div>
        <div><dt>系统</dt><dd>{page.server.platform}</dd></div>
        <div><dt>运行账户</dt><dd>{page.server.permission_context}</dd></div>
        <div className="server-material-dialog__identity"><dt>服务器标识</dt><dd><code>{page.server.server_ref}</code></dd></div>
      </dl>}
      <div className="server-material-dialog__path">
        <label htmlFor={`${id}-path`}>服务器绝对路径</label>
        <input id={`${id}-path`} ref={pathInput} value={path} onChange={event => setPath(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); if (!busy && path) void browse(path); } }} disabled={busy} autoComplete="off" spellCheck={false} placeholder="/absolute/server/path" />
        <div className="server-material-picker__actions">
          <button type="button" onClick={() => void browse(path)} disabled={busy || !path}>打开目录</button>
          <button type="button" disabled={busy || !path} onClick={() => void choose(path)}>选择此路径</button>
          {page && page.absolute_path !== "/" && <button type="button" onClick={() => void browse(parentPath(page.absolute_path))} disabled={busy}>上级目录</button>}
        </div>
      </div>
      <div aria-live="polite" className="server-material-dialog__status">
        {browser.phase === "loading" && <p>正在查看目录元数据…</p>}
        {inspection === "loading" && <p>正在检查所选路径…</p>}
        {browser.error && <p role="alert" className="server-material-picker__error">{browser.error}</p>}
        {selectionError && <p role="alert" className="server-material-picker__error">{selectionError}</p>}
      </div>
      {page && <section className="server-material-dialog__listing" aria-label="服务器目录条目" aria-busy={browser.phase === "loading"}>
        <div className="server-material-dialog__listing-heading"><code>{page.absolute_path}</code><small>原生顺序 · 子目录尚未展开</small></div>
        {page.entries.length === 0 && <p>此目录为空。</p>}
        <ul>
          {page.entries.map(entry => <li key={entry.absolute_path} className={candidate === entry.absolute_path ? "is-selected" : ""}>
            <label>
              <input type="radio" name={`${id}-candidate`} checked={candidate === entry.absolute_path} onChange={() => { setCandidate(entry.absolute_path); setSelectionError(null); }} disabled={busy || !permitted(entry.kind) || entry.availability !== "available"} />
              <span><strong>{entry.name}</strong><small>{entry.kind === "file" ? "文件" : entry.kind === "directory" ? "目录" : "不可选择的条目"}</small><code>{entry.absolute_path}</code></span>
            </label>
            {entry.kind === "directory" && entry.availability === "available" && <button type="button" onClick={() => void browse(entry.absolute_path)} disabled={busy} aria-label={`打开目录 ${entry.name}`}>打开</button>}
            {entry.availability !== "available" && <small className="server-material-picker__error">{workMaterialErrorMessage(new WorkMaterialError(entry.availability))}</small>}
          </li>)}
        </ul>
        {page.next_cursor && <button type="button" onClick={() => void browse(page.absolute_path, true)} disabled={busy}>加载更多条目</button>}
        {!page.next_cursor && <small>当前目录条目已全部列出。</small>}
      </section>}
      <div className="server-material-dialog__description">
        <label htmlFor={`${id}-description`}>原始材料说明</label>
        <textarea id={`${id}-description`} value={description} onChange={event => setDescription(event.target.value)} maxLength={4000} disabled={inspection === "loading"} rows={3} placeholder="说明材料的内容、用途或背景，提交时保留原文。" />
      </div>
      {candidate && <p className="server-material-dialog__chosen">当前候选 <code>{candidate}</code></p>}
      <div className="server-material-dialog__footer">
        <button type="button" onClick={close}>取消</button>
        {page && selectKind !== "file" && <button type="button" onClick={() => void choose(page.absolute_path)} disabled={busy}>选择当前目录</button>}
        <button type="button" onClick={() => void choose(candidate)} disabled={busy || !candidate}>使用所选材料</button>
      </div>
    </dialog>
  </div>;
}

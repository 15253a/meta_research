import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { fetchSnapshot, followProjection, type PublicSnapshot } from "./api";
import { QuestCreationWorkbench } from "./QuestCreation";
import "./styles.css";

const ownerLabels: Record<string, string> = {
  research_graph: "研究图谱",
  advancement_engine: "推进引擎",
  research_memory: "研究记忆",
  agent_runtime: "智能体运行时",
  human_collaboration: "人机协作",
};

const capabilityLabels: Record<string, string> = {
  quest_creation: "创建 Quest",
  quest_companion: "Quest Companion",
  stage_execution: "Stage 执行",
  writing: "Writing",
};

if (window.location.pathname === "/auth/launch") {
  window.history.replaceState(null, "", "/");
}

function App() {
  const [snapshot, setSnapshot] = useState<PublicSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [creationMode, setCreationMode] = useState<"current" | "new" | null>(
    () => {
      const panel = new URLSearchParams(window.location.search).get("panel");
      return panel === "new-quest" ? "new" : panel === "create-quest" ? "current" : null;
    },
  );
  const [streamCursor, setStreamCursor] = useState<number | null>(null);
  const reloadInFlight = useRef(false);
  const reloadQueued = useRef(false);

  const openCreation = () => {
    window.history.replaceState(null, "", "/?panel=create-quest");
    setCreationMode("current");
  };

  const openNewCreation = () => {
    window.history.replaceState(null, "", "/?panel=new-quest");
    setCreationMode("new");
  };

  const closeCreation = () => {
    window.history.replaceState(null, "", "/");
    setCreationMode(null);
  };

  const reload = useCallback(async (signal?: AbortSignal) => {
    if (reloadInFlight.current) {
      reloadQueued.current = true;
      return;
    }
    reloadInFlight.current = true;
    try {
      do {
        reloadQueued.current = false;
        try {
          const next = await fetchSnapshot(signal);
          setSnapshot((current) =>
            current && current.revision > next.revision ? current : next,
          );
          setStreamCursor((current) => current ?? next.revision);
          setError(null);
        } catch (caught) {
          if ((caught as Error).name !== "AbortError") {
            setError("无法读取本地 Snapshot。请确认 daemon 仍在运行，然后刷新页面。");
          }
        }
      } while (reloadQueued.current && !signal?.aborted);
    } finally {
      reloadInFlight.current = false;
      if (reloadQueued.current && !signal?.aborted) void reload();
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (streamCursor === null) return;
    return followProjection(
      streamCursor,
      () => void reload(),
      () => void reload(),
      setConnected,
    );
  }, [reload, streamCursor]);

  if (error && !snapshot) {
    return (
      <main className="fatal-state">
        <p className="eyebrow">本地连接不可用</p>
        <h1>研究空间暂时无法读取</h1>
        <p>{error}</p>
        <button type="button" onClick={() => void reload()}>
          重新读取
        </button>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main className="fatal-state" aria-busy="true">
        <p className="eyebrow">读取生产 Snapshot</p>
        <h1>正在连接本地研究空间</h1>
        <p>版本、readiness 与研究空间状态将在权威 Snapshot 返回后显示。</p>
      </main>
    );
  }

  const isReady = snapshot.readiness.status === "ready";
  const isEmpty = snapshot.research_space.status === "empty";
  const questCreation = snapshot.quest_creation.current;
  const questCompanion = snapshot.unavailable.find(
    (item) => item.capability === "quest_companion",
  );

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Meta-research 首页">
          <span className="brand-mark" aria-hidden="true">
            M∿
          </span>
          <span>
            <strong>Meta-research</strong>
            <small>本地研究光谱台</small>
          </span>
        </a>
        <div className="runtime-state" aria-live="polite">
          <span className={connected ? "live-dot connected" : "live-dot"} />
          <span>{connected ? "Projection 实时连接" : "正在连接 Projection"}</span>
          <code>rev {snapshot.revision}</code>
        </div>
      </header>

      <div className="spectral-axis" aria-hidden="true" />

      {error ? (
        <p className="runtime-warning" role="alert">
          {error} 正在保留最后一次单调 Snapshot。
        </p>
      ) : null}

      <main className="workspace">
        <section className="research-world" aria-labelledby="world-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Quest world</p>
              <h1 id="world-title">研究空间</h1>
            </div>
            <span className="version-chip">
              v{snapshot.product.version}
            </span>
          </div>

          <div className="tree-field">
            <div className="tree-origin" aria-hidden="true">
              <span className="orbit orbit-one" />
              <span className="orbit orbit-two" />
              <span className="origin-core" />
            </div>
            {!isReady ? (
              <div className="empty-copy" role="status">
                <p className="eyebrow">Typed unavailable</p>
                <h2>本地运行底座尚未就绪</h2>
                <p>Snapshot 已返回，但至少一项 readiness check 当前不可用。</p>
                <small>
                  {snapshot.readiness.checks
                    .filter((check) => check.status !== "ready")
                    .map((check) => `${check.name}:${check.status}`)
                    .join(" · ") || "readiness:unavailable"}
                </small>
                {questCreation ? (
                  <div className="research-actions">
                    <button
                      type="button"
                      className="empty-primary"
                      onClick={openCreation}
                    >
                      查看并恢复当前创建
                    </button>
                  </div>
                ) : null}
              </div>
            ) : isEmpty ? (
              <div className="empty-copy">
                <p className="eyebrow">Direct creation ready</p>
                <h2>{questCreation ? "首个 Quest 正在形成" : "这里还没有 Quest"}</h2>
                <p>
                  {questCreation
                    ? "继续审阅当前 Quest 基底、六字段问题与分层接纳状态。"
                    : "在一个连续窗口中定义 Goal、完成标准、关键配置与首问题方向。"}
                </p>
                <button
                  type="button"
                  className="empty-primary"
                  onClick={openCreation}
                >
                  {questCreation ? "继续创建" : "创建第一个 Quest"}
                </button>
                <small>
                  direct · {snapshot.quest_creation.status} · 材料 basis 尚未启用
                </small>
              </div>
            ) : (
              <div className="empty-copy">
                <p className="eyebrow">Research space</p>
                <h2>研究空间已有 Quest</h2>
                <p>
                  {snapshot.research_space.quest_count} 个 Quest ·{" "}
                  {snapshot.research_space.question_count} 个 Question
                </p>
                <div className="research-actions">
                  {questCreation ? (
                    <button
                      type="button"
                      className="empty-primary"
                      onClick={openCreation}
                    >
                      {questCreation.status === "completed"
                        ? "查看创建 receipts"
                        : "查看并恢复当前创建"}
                    </button>
                  ) : null}
                  {questCreation?.status === "completed" ? (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={openNewCreation}
                    >
                      创建新的 Quest
                    </button>
                  ) : null}
                </div>
              </div>
            )}
          </div>

          <section className="owner-strip" aria-labelledby="owners-title">
            <div className="strip-title">
              <h2 id="owners-title">权威状态</h2>
              <span>{isReady ? "全部就绪" : "存在不可用项"}</span>
            </div>
            <div className="owner-grid">
              {Object.entries(snapshot.owners).map(([name, owner]) => (
                <article key={name} className="owner-card">
                  <span className="owner-indicator" aria-hidden="true" />
                  <div>
                    <h3>{ownerLabels[name] ?? name}</h3>
                    <p>revision {owner.revision}</p>
                  </div>
                  <strong>{owner.status === "ready" ? "就绪" : "不可用"}</strong>
                </article>
              ))}
            </div>
          </section>
        </section>

        <aside className="companion" aria-labelledby="companion-title">
          <div className="companion-heading">
            <div>
              <p className="eyebrow">Always present</p>
              <h2 id="companion-title">Quest Companion</h2>
            </div>
            <span className="quiet-badge">
              {questCompanion?.status === "capability_unavailable"
                ? "尚未启用"
                : "状态未知"}
            </span>
          </div>

          <div className="companion-void">
            <span className="companion-glyph" aria-hidden="true">∿</span>
            <p>创建 Quest 后，Companion 会在这里解释研究并接收软影响。</p>
          </div>

          <label htmlFor="companion-message">与研究空间对话</label>
          <div className="message-box">
            <textarea
              id="companion-message"
              rows={3}
              disabled
              placeholder="Quest Companion 尚未启用"
            />
            <button type="button" disabled aria-label="发送消息">↗</button>
          </div>

          <section className="capability-list" aria-labelledby="capability-title">
            <h3 id="capability-title">当前能力</h3>
            {snapshot.unavailable.map((item) => (
              <div className="capability-row" key={item.capability}>
                <span>{capabilityLabels[item.capability] ?? item.capability}</span>
                <code>{item.status}</code>
              </div>
            ))}
          </section>
        </aside>
      </main>
      {creationMode ? (
        <QuestCreationWorkbench
          current={creationMode === "new" ? null : snapshot.quest_creation.current}
          onClose={closeCreation}
          onChanged={() => void reload()}
        />
      ) : null}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

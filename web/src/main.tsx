import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { fetchSnapshot, followProjection, type PublicSnapshot } from "./api";
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

  const reload = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await fetchSnapshot(signal);
      setSnapshot(next);
      setError(null);
    } catch (caught) {
      if ((caught as Error).name !== "AbortError") {
        setError("无法读取本地 Snapshot。请确认 daemon 仍在运行，然后刷新页面。");
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    const stop = followProjection(
      (revision) => {
        setSnapshot((current) =>
          current && revision > current.revision
            ? { ...current, revision }
            : current,
        );
      },
      () => void reload(),
      setConnected,
    );
    return () => {
      controller.abort();
      stop();
    };
  }, [reload]);

  if (error) {
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
  const questCreation = snapshot.unavailable.find(
    (item) => item.capability === "quest_creation",
  );
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
              </div>
            ) : isEmpty ? (
              <div className="empty-copy">
                <p className="eyebrow">Canonical empty advancement</p>
                <h2>这里还没有 Quest</h2>
                <p>
                  SQLite、对象存储和五个 Owner Interface 已就绪。首个 Quest
                  创建能力尚未在当前发行版启用。
                </p>
                <button type="button" disabled aria-describedby="quest-unavailable">
                  创建第一个 Quest
                </button>
                <small id="quest-unavailable">
                  {questCreation?.status ?? "capability_state_unavailable"} · 尚未启用
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
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

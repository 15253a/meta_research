import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  fetchSnapshot,
  followProjection,
  type PublicSnapshot,
  type UnavailableCapability,
} from "./api";
import { QuestCreationWorkbench } from "./QuestCreation";
import "./shell.css";

const capabilityLabels: Record<string, string> = {
  accepted_material_basis: "Research Asset",
  first_question_deepfetch: "首问题 DeepFetch",
  quest_creation: "创建 Quest",
  quest_companion: "Quest Companion",
  stage_execution: "Stage 执行",
  writing: "Writing",
};

const ownerLabels: Record<string, string> = {
  research_graph: "研究图谱",
  advancement_engine: "推进引擎",
  research_memory: "研究记忆",
  agent_runtime: "智能体运行时",
  human_collaboration: "人机协作",
};

type ShellState =
  | "loading"
  | "first-error"
  | "readiness-unavailable"
  | "ready-empty"
  | "ready-active";

if (window.location.pathname === "/auth/launch") {
  window.history.replaceState(null, "", "/");
}

function uniqueUnavailable(snapshot: PublicSnapshot | null): UnavailableCapability[] {
  if (!snapshot) return [];
  const entries = [
    {
      capability: "accepted_material_basis",
      ...snapshot.quest_creation.accepted_material_basis,
    },
    {
      capability: "first_question_deepfetch",
      ...snapshot.quest_creation.first_question_deepfetch,
    },
    ...snapshot.unavailable,
  ];
  return entries.filter(
    (entry, index) =>
      entries.findIndex((candidate) => candidate.capability === entry.capability) === index,
  );
}

function shellState(snapshot: PublicSnapshot | null, error: string | null): ShellState {
  if (!snapshot) return error ? "first-error" : "loading";
  if (snapshot.readiness.status !== "ready") return "readiness-unavailable";
  return snapshot.research_space.status === "empty" ? "ready-empty" : "ready-active";
}

function RailButton({
  label,
  glyph,
  active = false,
  unavailable = false,
  onClick,
}: {
  label: string;
  glyph: string;
  active?: boolean;
  unavailable?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      className={active ? "lumen-rail-button active" : "lumen-rail-button"}
      aria-label={label}
      title={unavailable ? `${label} · capability_unavailable` : label}
      disabled={unavailable}
      onClick={onClick}
    >
      <span aria-hidden="true">{glyph}</span>
      {unavailable ? <i aria-hidden="true" /> : null}
    </button>
  );
}

function LumenRail({
  canCreate,
  onCreate,
}: {
  canCreate: boolean;
  onCreate: () => void;
}) {
  return (
    <nav className="lumen-rail" aria-label="主导航" data-shell-region="rail">
      <RailButton label="Quest 总览" glyph="⌂" active />
      <RailButton label="问题树" glyph="树" unavailable />
      <RailButton label="Research Asset" glyph="▤" unavailable />
      <RailButton label="Writing" glyph="✎" unavailable />
      <RailButton label="历史" glyph="↺" unavailable />
      <RailButton label="HumanRequest" glyph="!" unavailable />
      <RailButton
        label="创建 Quest"
        glyph="＋"
        unavailable={!canCreate}
        onClick={onCreate}
      />
      <span className="lumen-rail-spacer" aria-hidden="true" />
      <RailButton label="用户入口" glyph="M" unavailable />
    </nav>
  );
}

function LoadingHero() {
  return (
    <>
      <p className="lumen-eyebrow">Production Snapshot · loading</p>
      <h1 id="workspace-title">
        正在连接本地研究空间。<br />
        <em>光谱台会留在这里。</em>
      </h1>
      <p>版本、readiness 与 canonical space 会从认证后的公开 Snapshot 进入同一个窗口。</p>
      <div className="lumen-inline-state" role="status">
        <span className="lumen-spinner" aria-hidden="true" />
        <div>
          <b>读取生产 Projection</b>
          <small>不会用浏览器 fixture 填充研究状态</small>
        </div>
      </div>
    </>
  );
}

function FirstErrorHero({ retry }: { retry: () => void }) {
  return (
    <>
      <p className="lumen-eyebrow coral">Snapshot · unavailable</p>
      <h1 id="workspace-title">
        研究空间暂时无法读取。<br />
        <em>Shell 仍然可辨认。</em>
      </h1>
      <p>daemon 没有返回首个 Snapshot。检查本地服务后，从这里重新读取。</p>
      <button className="lumen-primary" type="button" onClick={retry}>
        重新读取 Snapshot
      </button>
    </>
  );
}

function SnapshotHero({ snapshot }: { snapshot: PublicSnapshot }) {
  const ready = snapshot.readiness.status === "ready";
  const empty = snapshot.research_space.status === "empty";
  const creation = snapshot.quest_creation.current;

  if (!ready) {
    const failedChecks = snapshot.readiness.checks
      .filter((check) => check.status !== "ready")
      .map((check) => `${check.name}:${check.status}`);
    return (
      <>
        <p className="lumen-eyebrow coral">Readiness · typed unavailable</p>
        <h1 id="workspace-title">
          Snapshot 已返回。<br />
          <em>本地底座还未就绪。</em>
        </h1>
        <p>研究空间保持只读；修复 readiness 后，这个窗口会继续接收同一条 Projection。</p>
        <div className="lumen-inline-state unavailable" role="status">
          <span aria-hidden="true">!</span>
          <div>
            <b>readiness_unavailable</b>
            <small>{failedChecks.join(" · ") || "readiness:unavailable"}</small>
          </div>
        </div>
      </>
    );
  }

  if (empty) {
    return (
      <>
        <p className="lumen-eyebrow">Canonical empty · production Snapshot</p>
        <h1 id="workspace-title">
          {creation ? "首个 Quest 正在形成。" : "这里还没有 Quest。"}
          <br />
          <em>{creation ? "从同一个草案继续。" : "从一个清楚的问题开始。"}</em>
        </h1>
        <p>
          {creation
            ? "当前 initialization 已持久保存；使用左侧 ＋ 回到连续创建窗口。"
            : "使用左侧固定的 ＋ 创建入口，设定 Quest，并决定第一个研究问题。"}
        </p>
        <div className="lumen-inline-state ready" role="status">
          <span aria-hidden="true">✓</span>
          <div>
            <b>本地研究空间已就绪</b>
            <small>0 Quest · 0 Question · direct {snapshot.quest_creation.status}</small>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <p className="lumen-eyebrow">Research space · current Projection</p>
      <h1 id="workspace-title">
        研究已经在这里。<br />
        <em>从当前问题继续。</em>
      </h1>
      <p>Snapshot 只报告已接纳的研究事实；执行完成、资产接纳和 Stage 推进仍保持分离。</p>
      <div className="lumen-inline-state ready" role="status">
        <span aria-hidden="true">✓</span>
        <div>
          <b>{snapshot.research_space.quest_count} 个 Quest</b>
          <small>
            {snapshot.research_space.question_count} Question · {snapshot.research_space.foreground_cycle_count} foreground cycle
          </small>
        </div>
      </div>
    </>
  );
}

function WorkspaceMain({
  snapshot,
  state,
  error,
  streamInterrupted,
  retry,
}: {
  snapshot: PublicSnapshot | null;
  state: ShellState;
  error: string | null;
  streamInterrupted: boolean;
  retry: () => void;
}) {
  const unavailable = uniqueUnavailable(snapshot);
  return (
    <main
      id="main-content"
      className="lumen-main"
      data-shell-region="main"
      tabIndex={0}
      aria-labelledby="workspace-title"
      aria-busy={state === "loading"}
    >
      {(error || streamInterrupted) && snapshot ? (
        <div className="lumen-reconnect-warning" role="alert">
          <span aria-hidden="true">↺</span>
          <p>
            <b>Projection 连接中断，正在重连。</b>
            <small>继续显示最后一次可用的单调 Snapshot · rev {snapshot.revision}</small>
          </p>
        </div>
      ) : null}

      <section
        className={`lumen-hero ${state}`}
        role={state === "first-error" ? "alert" : undefined}
      >
        <span className="lumen-glow" aria-hidden="true" />
        <span className="lumen-comet" aria-hidden="true" />
        {state === "loading" ? <LoadingHero /> : null}
        {state === "first-error" ? <FirstErrorHero retry={retry} /> : null}
        {snapshot ? <SnapshotHero snapshot={snapshot} /> : null}
      </section>

      <div className="lumen-lower">
        <section className="lumen-card lumen-next-card" aria-labelledby="next-title">
          <header className="lumen-card-head">
            <b id="next-title">当前空间</b>
            <small>{snapshot ? `rev ${snapshot.revision}` : "等待 Snapshot"}</small>
          </header>
          <div className="lumen-path" aria-hidden="true">
            <span className="origin">MR</span>
            <i />
            <span className="destination">＋</span>
          </div>
          <h2>{state === "ready-empty" ? "第一个 Quest 从左侧入口开始" : "公开 Projection 决定这里显示什么"}</h2>
          <p>浏览不会写入 Owner。创建、授权与接纳始终经过各自的公开产品流程。</p>
        </section>

        <section className="lumen-card lumen-availability" aria-labelledby="availability-title">
          <header className="lumen-card-head">
            <b id="availability-title">能力可用性</b>
            <small>公开 Snapshot</small>
          </header>
          {unavailable.length ? (
            <ul>
              {unavailable.map((item) => (
                <li key={item.capability}>
                  <span>{capabilityLabels[item.capability] ?? item.capability}</span>
                  <code>{item.status}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p className="lumen-card-empty">
              {snapshot ? "Snapshot 没有报告 typed unavailable。" : "首个 Snapshot 返回后显示。"}
            </p>
          )}
          {snapshot ? (
            <details className="lumen-technical-details">
              <summary>查看运行详情</summary>
              <dl>
                <div><dt>版本</dt><dd>{snapshot.product.version}</dd></div>
                <div><dt>Readiness</dt><dd>{snapshot.readiness.status}</dd></div>
                {Object.entries(snapshot.owners).map(([name, owner]) => (
                  <div key={name}>
                    <dt>{ownerLabels[name] ?? name}</dt>
                    <dd>{owner.status} · r{owner.revision}</dd>
                  </div>
                ))}
              </dl>
            </details>
          ) : null}
        </section>
      </div>
    </main>
  );
}

function CompanionShell({ state }: { state: ShellState }) {
  const copy: Record<ShellState, { label: string; message: string }> = {
    loading: {
      label: "正在建立上下文",
      message: "我会在首个 Snapshot 返回后，用同一个窗口解释研究空间。",
    },
    "first-error": {
      label: "本地连接不可用",
      message: "首个 Snapshot 尚未返回；Shell 保持在位，修复 daemon 后可以重新读取。",
    },
    "readiness-unavailable": {
      label: "底座尚未就绪",
      message: "readiness 当前不可用。研究浏览保持只读，不会猜测或补写 Owner 状态。",
    },
    "ready-empty": {
      label: "研究空间已就绪",
      message: "这里还没有 Quest。使用左侧 ＋ 后，我会继续留在这个位置。",
    },
    "ready-active": {
      label: "跟随当前 Projection",
      message: "我会在这里解释研究状态；普通聊天不会直接写入领域事实。",
    },
  };

  return (
    <aside
      className="lumen-companion"
      aria-label="Quest Companion"
      data-shell-region="companion"
      tabIndex={0}
    >
      <header className="lumen-companion-head">
        <span className="lumen-orb" aria-hidden="true" />
        <div>
          <b>Quest Companion</b>
          <small>贯穿研究空间的高频入口</small>
        </div>
        <code>capability_unavailable</code>
      </header>
      <div className="lumen-chat" aria-live="polite">
        <article className="lumen-message">
          <small>{copy[state].label}</small>
          {copy[state].message}
        </article>
        <article className="lumen-proposal">
          <small>当前边界 · 无写入</small>
          <b>对话能力尚未启用</b>
          <p>这个固定位置不会被 capability list、Owner revision 或 receipt rail 取代。</p>
        </article>
      </div>
      <div className="lumen-compose">
        <div>
          <input aria-label="给 Quest Companion 发消息" disabled placeholder="Quest Companion 尚未启用" />
          <button type="button" disabled aria-label="发送消息">↑</button>
        </div>
        <small>普通聊天不会被猜成硬命令</small>
      </div>
    </aside>
  );
}

function App() {
  const [snapshot, setSnapshot] = useState<PublicSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [streamInterrupted, setStreamInterrupted] = useState(false);
  const [creationMode, setCreationMode] = useState<"current" | "new" | null>(
    () => {
      const panel = new URLSearchParams(window.location.search).get("panel");
      return panel === "new-quest" ? "new" : panel === "create-quest" ? "current" : null;
    },
  );
  const [streamCursor, setStreamCursor] = useState<number | null>(null);
  const [snapshotRetrySequence, setSnapshotRetrySequence] = useState(0);
  const reloadInFlight = useRef(false);
  const reloadQueued = useRef(false);

  const handleConnection = useCallback((next: boolean) => {
    setConnected(next);
    if (next) {
      setStreamInterrupted(false);
    } else {
      setStreamInterrupted(true);
    }
  }, []);

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
          setStreamCursor((current) =>
            current === null ? next.revision : Math.max(current, next.revision),
          );
          setSnapshotRetrySequence(0);
          setError(null);
        } catch (caught) {
          if ((caught as Error).name !== "AbortError") {
            setSnapshotRetrySequence((current) => current + 1);
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
    if (!error || snapshotRetrySequence === 0) return;
    const exponent = Math.min(snapshotRetrySequence - 1, 4);
    const timer = window.setTimeout(
      () => void reload(),
      Math.min(250 * 2 ** exponent, 4_000),
    );
    return () => window.clearTimeout(timer);
  }, [error, reload, snapshotRetrySequence]);

  useEffect(() => {
    if (streamCursor === null) return;
    return followProjection(
      streamCursor,
      () => void reload(),
      () => void reload(),
      handleConnection,
    );
  }, [handleConnection, reload, streamCursor]);

  const state = shellState(snapshot, error);
  const canCreate = snapshot?.readiness.status === "ready";
  const openCreation = () => {
    if (!canCreate) return;
    window.history.replaceState(null, "", "/?panel=create-quest");
    setCreationMode("current");
  };
  const closeCreation = () => {
    window.history.replaceState(null, "", "/");
    setCreationMode(null);
  };

  return (
    <>
      <a className="lumen-skip" href="#main-content">跳到主要内容</a>
      <div className="lumen-shell" data-testid="product-shell" data-shell-state={state}>
        <header className="lumen-header" data-shell-region="header">
          <div className="lumen-brand" aria-label="Meta-research">
            <span className="lumen-logo" aria-hidden="true">MR</span>
            <div><b>Meta Research</b><small>Lumen workspace</small></div>
          </div>
          <div className="lumen-quest-context">
            <small>{snapshot?.research_space.status === "active" ? "QUEST / CURRENT" : "QUEST / NEW SPACE"}</small>
            <b>{snapshot?.research_space.status === "active" ? "当前研究空间" : "等待第一个 Quest"}</b>
          </div>
          <div className={`lumen-connection ${connected ? "connected" : ""}`} aria-live="polite">
            <i aria-hidden="true" />
            <span>
              {error || streamInterrupted ? "Projection 正在重连" : connected ? "Projection 实时连接" : snapshot ? "正在连接 Projection" : "读取 Snapshot"}
            </span>
            {snapshot ? <code>rev {snapshot.revision}</code> : null}
          </div>
        </header>
        <LumenRail canCreate={Boolean(canCreate)} onCreate={openCreation} />
        <WorkspaceMain
          snapshot={snapshot}
          state={state}
          error={error}
          streamInterrupted={streamInterrupted}
          retry={() => void reload()}
        />
        <CompanionShell state={state} />
      </div>
      {creationMode && snapshot ? (
        <QuestCreationWorkbench
          current={creationMode === "new" ? null : snapshot.quest_creation.current}
          onClose={closeCreation}
          onChanged={() => void reload()}
        />
      ) : null}
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

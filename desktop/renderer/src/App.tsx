import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApprovalCard,
  ContextCards,
  DiffReview,
  matchesRequestFilter,
  matchesRunFilter,
  NewTask,
  PixelLoader,
  PlanReview,
  ProjectSidebar,
  TaskInspector,
  RecommendationCard,
  RunActions,
  TERMINAL_STATUSES,
  ToolChip,
  clampTerminalSplit,
  newlyReadyPlan,
  unresolvedRunCounts,
} from "./components";
import { TerminalPanel } from "./TerminalPanel";
import type { RunFilter } from "./components";
import type {
  AttemptPayload,
  CommanderRequest,
  CommanderRequestDetail,
  DiffPayload,
  LibraryPayload,
  PlanPrompt,
  RunDetail,
  RunSummary,
  SystemStatus,
} from "./types";

type Surface = "library" | "work" | "review" | "new-task";
type Filter = RunFilter;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}

export default function App() {
  const [surface, setSurface] = useState<Surface>("work");
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [library, setLibrary] = useState<LibraryPayload | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [selectedRun, setSelectedRun] = useState<RunSummary | null>(null);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [attempts, setAttempts] = useState<AttemptPayload | null>(null);
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [request, setRequest] = useState<CommanderRequestDetail | null>(null);
  const [planPrompt, setPlanPrompt] = useState<PlanPrompt | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [objective, setObjective] = useState("");
  const [revisionOf, setRevisionOf] = useState("");
  const [approvalMode, setApprovalMode] = useState<"supervised" | "yolo">("supervised");
  const [workspaceTarget, setWorkspaceTarget] = useState<"worktree" | "checkout">("worktree");
  const [split, setSplit] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [terminalVisible, setTerminalVisible] = useState(true);
  const [terminalSplit, setTerminalSplit] = useState(0.5);
  const [focusPaths, setFocusPaths] = useState<string[]>([]);
  const desktop = typeof window !== "undefined" ? window.desktop : undefined;
  const seenPlans = useRef<Set<string> | null>(null);
  const selectedRunRef = useRef<RunSummary | null>(null);
  const requestIdRef = useRef<string | null>(null);
  const projectKey = library?.activeWorkspace.projectRoot
    || library?.activeWorkspace.workspaceRoot
    || "";
  selectedRunRef.current = selectedRun;
  requestIdRef.current = request?.request.requestId || null;

  const refresh = useCallback(async () => {
    try {
      const [status, nextLibrary] = await Promise.all([
        api<SystemStatus>("/api/status"),
        api<LibraryPayload>("/api/library"),
      ]);
      setSystem(status);
      setLibrary(nextLibrary);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const loadRun = useCallback(async (run: Pick<RunSummary, "planId" | "sessionId">) => {
    const base = `/api/runs/${encodeURIComponent(run.planId)}/${encodeURIComponent(run.sessionId)}`;
    const [nextDetail, nextDiff] = await Promise.all([
      api<RunDetail>(base),
      api<DiffPayload>(`${base}/diff`).catch(() => null),
    ]);
    setSelectedRun(nextDetail.run);
    setDetail(nextDetail);
    if (nextDiff) setDiff(nextDiff);
    setSelectedTask((current) => {
      const ids = Object.entries(nextDetail.tasks);
      const actionable = ids.find(([, task]) => task.status.includes("approval"))
        || ids.find(([, task]) => task.status === "running")
        || ids.find(([, task]) => task.status === "rejected" || task.status.includes("failed"));
      const nextId = actionable?.[0] || null;
      if (!current || !nextDetail.tasks[current]) return nextId;
      const status = nextDetail.tasks[current].status;
      if (nextId && (status === "pending" || status === "completed" || status === "failed")) {
        return nextId;
      }
      return current;
    });
    return nextDetail;
  }, []);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      await refresh();
      const run = selectedRunRef.current;
      if (run && !cancelled) {
        try {
          await loadRun(run);
        } catch {
          // Keep the last painted run if a poll misses.
        }
      }
      const requestId = requestIdRef.current;
      if (requestId && !cancelled) {
        try {
          const next = await api<CommanderRequestDetail>(
            `/api/commander/requests/${encodeURIComponent(requestId)}`,
          );
          if (!cancelled) setRequest(next);
        } catch {
          // Keep the last painted request if a poll misses.
        }
      }
    };
    void tick();
    const live = Boolean(
      detail?.run.status && !TERMINAL_STATUSES.includes(detail.run.status),
    );
    const timer = window.setInterval(tick, live ? 1000 : 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refresh, loadRun, detail?.run.status, selectedRun?.sessionId, request?.request.requestId]);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
      if (event.key === "u") setSplit((value) => !value);
      if (!diff?.files.length || !["j", "k"].includes(event.key)) return;
      const headers = Array.from(document.querySelectorAll<HTMLElement>(".diff-file"));
      const current = headers.findIndex((node) => node.getBoundingClientRect().top >= 0);
      const next = event.key === "j"
        ? Math.min(headers.length - 1, Math.max(0, current + 1))
        : Math.max(0, current - 1);
      headers[next]?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [diff]);

  const visibleRuns = useMemo(
    () => (library?.runs || []).filter((run) => matchesRunFilter(run, filter, query)),
    [library, filter, query],
  );

  const visibleRequests = useMemo(
    () => (library?.requests || []).filter((request) => matchesRequestFilter(request, filter, query)),
    [library, filter, query],
  );

  async function openRun(run: RunSummary, target: Surface = "work", keepRequest = false) {
    setBusy(true);
    setSelectedRun(run);
    setSurface(target);
    setSelectedTask(null);
    setAttempts(null);
    if (!keepRequest) {
      setRequest(null);
      setPlanPrompt(null);
    }
    try {
      await loadRun(run);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function selectTask(taskId: string) {
    setSelectedTask(taskId);
    if (!selectedRun) return;
    try {
      setAttempts(await api<AttemptPayload>(
        `/api/runs/${encodeURIComponent(selectedRun.planId)}/${encodeURIComponent(selectedRun.sessionId)}/attempts/${encodeURIComponent(taskId)}`,
      ));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function createRequest() {
    if (!objective.trim()) return;
    setBusy(true);
    try {
      const created = await api<CommanderRequest>("/api/commander/requests", {
        method: "POST",
        body: JSON.stringify({
          objective: objective.trim(),
          constraints: [],
          ...(revisionOf.trim() ? { revisionOf: revisionOf.trim() } : {}),
        }),
      });
      setObjective("");
      setRevisionOf("");
      await refresh();
      await openRequest(created.requestId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function openRequest(requestId: string) {
    setBusy(true);
    setSurface("work");
    const id = encodeURIComponent(requestId);
    try {
      const next = await api<CommanderRequestDetail>(`/api/commander/requests/${id}`);
      setRequest(next);
      setPlanPrompt(await api<PlanPrompt>(`/api/commander/requests/${id}/prompt`).catch(() => null));
      const sessionRef = next.request.sessionRef;
      if (sessionRef && sessionRef.includes("/")) {
        const [planId, sessionId] = sessionRef.split("/");
        const listed = (library?.runs || []).find(
          (run) => run.planId === planId && run.sessionId === sessionId,
        );
        await loadRun(listed || { planId, sessionId, status: next.request.status });
      } else {
        setDetail(null);
        setSelectedRun(null);
      }
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    seenPlans.current = null;
  }, [projectKey]);

  useEffect(() => {
    const sessionRef = request?.request.sessionRef;
    if (!sessionRef || selectedRun || !sessionRef.includes("/")) return;
    const [planId, sessionId] = sessionRef.split("/");
    void loadRun({ planId, sessionId });
  }, [request?.request.sessionRef, selectedRun, loadRun]);

  useEffect(() => {
    if (!library) return;
    const ready = newlyReadyPlan(seenPlans.current, library.requests || []);
    seenPlans.current = ready.seen;
    if (ready.discovered) void openRequest(ready.discovered.requestId);
  }, [library]);

  useEffect(() => {
    if (typeof library?.uiState?.terminalVisible === "boolean") {
      setTerminalVisible(library.uiState.terminalVisible);
    }
  }, [projectKey, library?.uiState?.terminalVisible]);

  useEffect(() => {
    if (typeof library?.uiState?.terminalSplit === "number") {
      setTerminalSplit(clampTerminalSplit(library.uiState.terminalSplit));
    }
    if (Array.isArray(library?.uiState?.focusPaths)) {
      setFocusPaths(library.uiState.focusPaths.filter((item) => typeof item === "string"));
    }
  }, [projectKey]);

  useEffect(() => {
    return desktop?.onWorkspaceChanged((next) => {
      setLibrary(next as LibraryPayload);
      seenPlans.current = null;
    });
  }, [desktop]);

  useEffect(() => {
    return desktop?.terminal.onToggle(() => {
      void setTerminalHidden(terminalVisible);
    });
  }, [desktop, terminalVisible]);

  async function persistUiState(patch: {
    terminalVisible?: boolean;
    terminalSplit?: number;
    focusPaths?: string[];
  }) {
    try {
      await api("/api/ui-state", {
        method: "POST",
        body: JSON.stringify(patch),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function setTerminalHidden(currentlyVisible: boolean) {
    const visible = !currentlyVisible;
    setTerminalVisible(visible);
    await persistUiState({ terminalVisible: visible });
  }

  function openNewTask() {
    setSurface("new-task");
    setRequest(null);
    setSelectedRun(null);
    setDetail(null);
    setDiff(null);
    setSelectedTask(null);
    setAttempts(null);
    setPlanPrompt(null);
    if (desktop && !terminalVisible) {
      setTerminalVisible(true);
      void persistUiState({ terminalVisible: true });
    }
  }

  async function openFolder() {
    if (!desktop) return;
    try {
      const next = await desktop.openFolder();
      if (next && typeof next === "object") {
        setLibrary(next as LibraryPayload);
        seenPlans.current = null;
        setSurface("work");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function approveRequest() {
    const planDigest = request?.plan?.digest || request?.request?.planDigest;
    if (!request || !planDigest) return;
    const id = encodeURIComponent(request.request.requestId);
    setBusy(true);
    try {
      await api(`/api/commander/requests/${request.request.requestId}/approve-run`, {
        method: "POST",
        body: JSON.stringify({
          planDigest,
          executionDigest: request.executionPreview?.executionDigest,
          approvalMode,
          workspaceTarget,
          maxRepair: 1,
        }),
      });
      await refresh();
      const next = await api<CommanderRequestDetail>(`/api/commander/requests/${id}`);
      setRequest(next);
      const sessionRef = next.request.sessionRef;
      if (sessionRef && sessionRef.includes("/")) {
        const [planId, sessionId] = sessionRef.split("/");
        await loadRun({ planId, sessionId });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function decideArtifact(taskId: string, action: "apply" | "reject" | "verify") {
    if (!selectedRun || !detail?.artifacts?.[taskId]) return;
    const artifact = detail.artifacts[taskId];
    setBusy(true);
    try {
      await api(
        `/api/runs/${selectedRun.planId}/${selectedRun.sessionId}/artifacts/${taskId}/${action}`,
        {
          method: "POST",
          body: JSON.stringify({ artifactDigest: artifact.manifest.sha256 }),
        },
      );
      await openRun(selectedRun, surface);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function runAction(action: "resume" | "retry" | "cleanup") {
    if (!selectedRun) return;
    setBusy(true);
    try {
      const base = `/api/runs/${selectedRun.planId}/${selectedRun.sessionId}`;
      const body =
        action === "retry"
          ? JSON.stringify({
              maxRepair: 1,
              executionDigest: detail?.retryExecutionPreview?.executionDigest,
            })
          : JSON.stringify({});
      await api(`${base}/${action}`, { method: "POST", body });
      await refresh();
      await openRun(selectedRun, surface);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  const activeArtifact = selectedTask ? detail?.artifacts?.[selectedTask] : null;

  const showDock = Boolean(desktop && terminalVisible && surface !== "new-task");
  const showPaneTerminal = Boolean(desktop && terminalVisible && surface === "new-task");

  return (
    <div className={`app-shell ${showDock ? "with-terminal" : ""} ${surface === "new-task" ? "new-task" : ""} ${surface === "work" ? "work-session" : ""}`}>
      <aside className="sidebar">
        <ProjectSidebar
          projectName={library?.activeWorkspace.workspaceRoot.split("/").pop() || "No project"}
          requests={library?.requests || []}
          runs={library?.runs || []}
          selectedRequestId={request?.request.requestId || null}
          selectedRunKey={selectedRun ? `${selectedRun.planId}/${selectedRun.sessionId}` : null}
          canOpenFolder={Boolean(desktop)}
          onOpenFolder={() => void openFolder()}
          onSelectRequest={(id) => void openRequest(id)}
          onSelectReview={(run) => {
            const live = !TERMINAL_STATUSES.includes(run.status);
            void openRun(run, live ? "work" : "review");
          }}
          onNewTask={openNewTask}
        />
        <div className="sidebar-footer">
          <span className={`state-dot ${system?.ready ? "completed" : "failed"}`} />
          <span><strong>{system?.ready ? "Model ready" : "Model unavailable"}</strong><small>{system?.model.repository || "Checking…"}</small></span>
          {desktop && (
            <button
              type="button"
              className="terminal-toggle"
              aria-pressed={terminalVisible}
              aria-label={terminalVisible ? "Hide terminal" : "Show terminal"}
              onClick={() => void setTerminalHidden(terminalVisible)}
            >
              ⌁
            </button>
          )}
          <button aria-label="Toggle theme" className="theme-toggle" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            <i />
          </button>
        </div>
      </aside>

      <main>
        {error && <button className="error-banner" onClick={() => setError("")}>{error} ×</button>}
        {busy && <div className="busy-overlay"><PixelLoader label="Working" /></div>}
        {surface === "library" && (
          <Library
            runs={visibleRuns}
            requests={visibleRequests}
            filter={filter}
            setFilter={setFilter}
            query={query}
            setQuery={setQuery}
            openRun={openRun}
            openRequest={openRequest}
          />
        )}
        {surface === "work" && (
          <Work
            detail={detail}
            request={request}
            planPrompt={planPrompt}
            objective={objective}
            setObjective={setObjective}
            revisionOf={revisionOf}
            setRevisionOf={setRevisionOf}
            approvalMode={approvalMode}
            setApprovalMode={setApprovalMode}
            workspaceTarget={workspaceTarget}
            setWorkspaceTarget={(value: "worktree" | "checkout") => {
              if (value === "checkout" && approvalMode !== "yolo") return;
              setWorkspaceTarget(value);
            }}
            createRequest={createRequest}
            approveRequest={approveRequest}
            selectTask={selectTask}
            selectedTask={selectedTask}
            activeArtifact={activeArtifact}
            decideArtifact={decideArtifact}
            runAction={runAction}
          />
        )}
        {surface === "new-task" && (
          <NewTask
            skill={system?.skill}
            skills={system?.skills}
            configPath={library?.activeWorkspace.configPath}
            desktop={Boolean(desktop)}
            terminalVisible={terminalVisible}
            terminalSplit={terminalSplit}
            onSplitChange={setTerminalSplit}
            onSplitCommit={(value) => {
              const next = clampTerminalSplit(value);
              setTerminalSplit(next);
              void persistUiState({ terminalSplit: next });
            }}
            terminal={showPaneTerminal ? <TerminalPanel visible /> : undefined}
            focusPaths={focusPaths}
            onFocusPaths={(paths) => {
              setFocusPaths(paths);
              void persistUiState({ focusPaths: paths });
            }}
            onOpenFolder={() => void openFolder()}
            canOpenFolder={Boolean(desktop)}
          />
        )}
        {surface === "review" && (
          <Review
            run={selectedRun}
            diff={diff}
            attempts={attempts}
            split={split}
            setSplit={setSplit}
            detail={detail}
            selectTask={selectTask}
            selectedTask={selectedTask}
          />
        )}
      </main>
      {showDock && <TerminalPanel visible={terminalVisible} />}
    </div>
  );
}

function Library({ runs, requests, filter, setFilter, query, setQuery, openRun, openRequest }: {
  runs: RunSummary[]; requests: CommanderRequest[]; filter: Filter;
  setFilter: (value: Filter) => void; query: string; setQuery: (value: string) => void;
  openRun: (run: RunSummary, target?: Surface) => void; openRequest: (id: string) => void;
}) {
  return (
    <section className="surface library-surface">
      <header className="surface-header"><div><p className="kicker">Production workspace</p><h1>Local agent work</h1></div><ToolChip>{runs.length} runs</ToolChip></header>
      <label className="search"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search plans, sessions, or objectives" /></label>
      <div className="filters">
        {(["all", "running", "approval", "finished"] as Filter[]).map((value) => (
          <button className={filter === value ? "active" : ""} onClick={() => setFilter(value)} key={value}>{value}</button>
        ))}
      </div>
      {!!requests.length && <section className="request-strip"><p className="kicker">Planning inbox</p>{requests.map((item) => <button key={item.requestId} onClick={() => openRequest(item.requestId)}><span><strong>{item.objective || item.requestId}</strong><small>{item.requestId}</small></span><ToolChip>{item.status}</ToolChip></button>)}</section>}
      <div className="records-table">
        <div className="records-head"><span>Run</span><span>Started</span><span>Status</span><span>Actions</span></div>
        {runs.map((run) => <div className="record-row" key={`${run.planId}/${run.sessionId}`}>
          <span><strong>{run.objective || run.planId}</strong><small>{run.planId} · {run.sessionId}</small></span>
          <span>{formatDate(run.startedAt)}</span>
          <span className="record-status">
            <ToolChip tone={run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "warn"}>{run.status}</ToolChip>
            {typeof run.total === "number" && run.total > 0 && (
              <ToolChip tone={run.completed === run.total ? "good" : "neutral"}>{run.completed ?? 0}/{run.total} tasks</ToolChip>
            )}
            {unresolvedRunCounts(run).map(([status, count]) => (
              <ToolChip key={status} tone={status === "rejected" || status.includes("failed") ? "bad" : "warn"}>
                {count} {status.replace(/_/g, " ")}
              </ToolChip>
            ))}
            {run.pauseReason && <small className="record-reason">{run.pauseReason.replace(/_/g, " ")}</small>}
          </span>
          <span className="row-actions"><button onClick={() => openRun(run, "work")}>Open</button><button onClick={() => openRun(run, "review")}>Review</button></span>
        </div>)}
        {!runs.length && <div className="empty-card">No runs match this filter.</div>}
      </div>
    </section>
  );
}

function Work({ detail, request, planPrompt, objective, setObjective, revisionOf, setRevisionOf, approvalMode, setApprovalMode, workspaceTarget, setWorkspaceTarget, createRequest, approveRequest, selectTask, selectedTask, activeArtifact, decideArtifact, runAction }: any) {
  return (
    <section className="surface work-surface">
      <header className="surface-header"><div><p className="kicker">Agent tasks</p><h1>{detail?.run.objective || request?.request?.objective || "What should the swarm do?"}</h1></div>{detail && <ToolChip tone={detail.run.status === "completed" ? "good" : "warn"}>{detail.run.status}</ToolChip>}</header>
      <div className={`work-layout${detail ? " has-panel" : ""}`}>
        <div className="work-main">
          {detail?.plan ? (
            <PlanReview
              plan={detail.plan}
              levels={detail.levels}
              taskStates={detail.tasks}
              artifacts={detail.artifacts}
              selectedTaskId={selectedTask}
              onSelectTask={selectTask}
              onDecideTask={decideArtifact}
              executionDigest={request?.executionPreview?.executionDigest}
              title="Plan"
            />
          ) : request?.plan ? (
            <PlanReview
              plan={request.plan}
              planPrompt={planPrompt}
              executionDigest={request.executionPreview?.executionDigest}
              title={request.request.status === "launched" ? "Plan" : "Plan awaiting approval"}
            />
          ) : request ? (
            <PlanningRequest request={request} />
          ) : (
            <div className="work-empty"><PixelLoader label="Ready for a bounded objective" /><p>Create a planning request. The frontier designs the graph; local MLX workers execute it.</p></div>
          )}
          {request?.plan && request.request.status !== "launched" && <>
            <RecommendationCard
              approvalMode={approvalMode}
              workspaceTarget={workspaceTarget}
              onApprovalMode={(value) => {
                setApprovalMode(value);
                if (value !== "yolo" && workspaceTarget === "checkout") {
                  setWorkspaceTarget("worktree");
                }
              }}
              onWorkspaceTarget={setWorkspaceTarget}
            />
            <ApprovalCard title="Approve this plan?" detail={`The validated plan contains ${request.plan.tasks?.length || 0} bounded tasks. The displayed plan and execution digests will be bound together.`} primaryLabel="Continue" onPrimary={approveRequest} />
          </>}
          {detail && <RunActions detail={detail} onAction={runAction} />}
          {!request && !detail && (
            <div className="prompt-bar">
              <textarea value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Describe a bounded result…  @ sources  / commands" onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") createRequest(); }} />
              <div><ToolChip>Frontier plan</ToolChip><input className="continue-input" value={revisionOf} onChange={(event) => setRevisionOf(event.target.value)} placeholder="Continue plan/session (optional)" /><span>⌘↵ to send</span><button disabled={!objective.trim()} onClick={createRequest}>↑</button></div>
            </div>
          )}
        </div>
        {detail && (
          <TaskInspector
            taskId={selectedTask}
            task={selectedTask ? detail.tasks[selectedTask] : undefined}
            artifact={activeArtifact || undefined}
            onRefresh={selectTask}
            onDecide={decideArtifact}
          />
        )}
      </div>
    </section>
  );
}

function PlanningRequest({ request }: { request: CommanderRequestDetail }) {
  return <div className="planning-card">
    <PixelLoader label={request.plan ? `Plan validated · ${request.request.status}` : "Awaiting frontier plan"} />
    <code>{request.request.requestId}</code>
    {!request.plan && <p>Run the mlx-swarm-commander skill in the terminal for this project.</p>}
    {request.executionError && <pre>{request.executionError}</pre>}
    {request.validationError ? <pre>{JSON.stringify(request.validationError, null, 2)}</pre> : null}
  </div>;
}

function Review({ run, diff, attempts, split, setSplit, detail, selectTask, selectedTask }: {
  run: RunSummary | null; diff: DiffPayload | null; attempts: AttemptPayload | null;
  split: boolean; setSplit: (value: boolean) => void; detail: RunDetail | null;
  selectTask: (id: string) => void; selectedTask: string | null;
}) {
  const [tab, setTab] = useState<"changes" | "prompts">("changes");
  return <section className="surface review-surface">
    <header className="surface-header"><div><p className="kicker">Immutable review</p><h1>{run?.objective || run?.planId || "Select a review in the sidebar"}</h1></div>{diff && <span className="diff-stats"><b>+{diff.summary.additions}</b><i>−{diff.summary.deletions}</i></span>}</header>
    <div className="review-toolbar"><div><button className={tab === "changes" ? "active" : ""} onClick={() => setTab("changes")}>Files changed</button><button className={tab === "prompts" ? "active" : ""} onClick={() => setTab("prompts")}>Prompts</button></div>{tab === "changes" && <button onClick={() => setSplit(!split)}>{split ? "Unified" : "Split"} view <kbd>U</kbd></button>}</div>
    {!run && <div className="empty-card">Choose a review from the opened project in the sidebar.</div>}
    {run && tab === "changes" && <DiffReview diff={diff} split={split} />}
    {run && tab === "prompts" && <div className="prompt-review">
      <aside>{Object.keys(detail?.tasks || {}).map((id) => <button className={selectedTask === id ? "active" : ""} onClick={() => selectTask(id)} key={id}>{id}</button>)}</aside>
      <ContextCards payload={attempts} />
    </div>}
  </section>;
}

function formatDate(value?: string) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

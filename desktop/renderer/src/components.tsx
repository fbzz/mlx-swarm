"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import type {
  AttemptPayload,
  CommanderRequest,
  RunSummary,
  DiffPayload,
  PlanPayload,
  PlanPrompt,
  PlanTask,
  RunDetail,
  SkillStatus,
  TaskState,
  CodebaseMap,
  CodebaseMapNode,
} from "./types";

export type RunFilter = "all" | "running" | "approval" | "finished";

export const ACTIVE_STATUSES = ["pending", "running"];
// A run is finished once it stops advancing. That includes "partial": every
// task can be completed while the session is partial because plan level
// integration verification failed after them.
export const TERMINAL_STATUSES = ["completed", "partial", "failed"];
// A request leaves the planning inbox the moment it launches: from then on it
// is a run, and it carries a sessionRef pointing at that run.
export const PENDING_REQUEST_STATUSES = ["awaiting_plan", "plan_ready", "plan_invalid"];

function matchesQuery(haystack: string, query: string) {
  return haystack.toLowerCase().includes(query.trim().toLowerCase());
}

export function matchesRunFilter(run: RunSummary, filter: RunFilter, query: string) {
  const text = `${run.planId} ${run.sessionId} ${run.objective || ""}`;
  if (!matchesQuery(text, query)) return false;
  if (filter === "all") return true;
  if (filter === "running") return ACTIVE_STATUSES.includes(run.status);
  if (filter === "approval") return run.status.includes("approval");
  return TERMINAL_STATUSES.includes(run.status);
}

export function matchesRequestFilter(
  request: CommanderRequest,
  filter: RunFilter,
  query: string,
) {
  // "running" and "finished" describe runs, so the inbox has nothing to add.
  if (filter === "running" || filter === "finished") return false;
  if (request.sessionRef) return false;
  if (!PENDING_REQUEST_STATUSES.includes(request.status)) return false;
  return matchesQuery(`${request.requestId} ${request.objective || ""}`, query);
}

export function PixelLoader({ label }: { label: string }) {
  return (
    <div className="pixel-loader" role="status">
      <span className="pixel-grid" aria-hidden="true">
        {Array.from({ length: 9 }, (_, index) => (
          <i key={index} style={{ animationDelay: `${index * 70}ms` }} />
        ))}
      </span>
      <span className="shimmer">{label}</span>
    </div>
  );
}

export function ToolChip({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  return <span className={`tool-chip ${tone}`}>{children}</span>;
}

// A task that failed or was rejected carries its reason in gateResult.violations
// (and sometimes in error). Both are in the run payload already; without this the
// row shows a red chip and the operator has to guess.
export function taskFailureReasons(task: TaskState): string[] {
  const reasons: string[] = [];
  if (task.error) reasons.push(task.error);
  (task.gateResult?.violations || []).forEach((violation) => {
    const help = explainViolation(violation.kind);
    reasons.push([violation.message || violation.id, help].filter(Boolean).join(" "));
  });
  return reasons;
}

export function isTaskUnresolved(task: TaskState) {
  return task.status === "rejected" || task.status.includes("failed") || task.status === "blocked";
}

// Every task status other than completed, for the run rows in the library.
export function unresolvedRunCounts(run: RunSummary): Array<[string, number]> {
  return Object.entries(run.counts || {}).filter(([status]) => status !== "completed");
}

// What a gate violation kind actually means, and whether re-running can clear
// it. A "retry" replays the same plan, so a contract or schema mismatch needs a
// corrected plan instead — saying so stops an operator burning a rerun on it.
const VIOLATION_HELP: Record<string, string> = {
  workspace:
    "The artifact did not match the contract for its declared artifactType. A retry replays the same plan, so this clears only after the plan is corrected.",
  size:
    "Generation reached its output token ceiling. A retry raises the bounded ceiling once; if it truncates again the task has to be split.",
  "required-pattern":
    "The output was missing something the gate requires. A retry re-runs the task with the gate feedback attached.",
  "forbidden-pattern":
    "The output contained something the gate forbids. A retry re-runs the task with the gate feedback attached.",
  format:
    "The output was not in the format the gate declared. A retry re-runs the task with the gate feedback attached.",
  schema:
    "The output did not match the required JSON shape. A retry re-runs the task with the gate feedback attached.",
  repair:
    "The repair attempt reproduced the previous output, so the runtime stopped rather than replay it. Change the plan before running again.",
  verification:
    "A verification profile failed after the artifact was applied.",
};

export function explainViolation(kind?: string) {
  return (kind && VIOLATION_HELP[kind]) || "";
}

function SpinnerRing({
  active,
  children,
}: {
  active?: boolean;
  children?: React.ReactNode;
}) {
  const size = 24;
  const stroke = 2;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <span
      className="relative inline-flex shrink-0 items-center justify-center"
      style={{ width: size, height: size }}
    >
      <svg
        width={size}
        height={size}
        className="absolute inset-0"
        style={active ? { animation: "spin 1.1s linear infinite" } : undefined}
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="var(--line)"
          strokeWidth={stroke}
        />
        {active && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke="var(--ink-3)"
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={`${c * 0.28} ${c * 0.72}`}
          />
        )}
      </svg>
      <span className="relative text-[10.5px] font-semibold text-ink tabular-nums">
        {children}
      </span>
    </span>
  );
}

function Badge({
  tone,
  children,
}: {
  tone: "red" | "green";
  children: React.ReactNode;
}) {
  return (
    <span
      className={`flex size-5.5 shrink-0 items-center justify-center rounded-full text-white ${
        tone === "red" ? "bg-red" : "bg-green"
      }`}
      style={{ animation: "pop-in 300ms cubic-bezier(0.23,1,0.32,1) both" }}
    >
      {children}
    </span>
  );
}

const XIcon = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round">
    <path d="M18 6L6 18M6 6l12 12" />
  </svg>
);
const CheckIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 6L9 17l-5-5" />
  </svg>
);
const RetryIcon = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
  </svg>
);

function taskKind(status: string): "pending" | "failed" | "done" | "running" {
  if (status === "completed") return "done";
  if (status.includes("failed") || status === "rejected") return "failed";
  if (status === "running" || status.includes("approval")) return "running";
  return "pending";
}

function taskAmount(task: TaskState, definition?: PlanTask) {
  const outcome = task.normalizedOutput || task.output || "";
  if (outcome) {
    return `${task.artifact?.artifactType || "output"} · ${outcome.length.toLocaleString()} chars`;
  }
  return definition?.role || task.role || "";
}

function taskDetails(task: TaskState, definition?: PlanTask) {
  const details: Array<{ label: string; meta: string; tone?: "bad" }> = [];
  const outcome = task.normalizedOutput || task.output || "";
  const preview = outcome
    .trim()
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 2)
    .join(" · ");
  if (preview) details.push({ label: preview, meta: "" });
  if (definition?.role || task.role) {
    details.push({ label: "Role", meta: definition?.role || task.role || "" });
  }
  if (task.gateResult) {
    details.push({
      label: `Gate ${task.gateResult.passed ? "passed" : "failed"}`,
      meta: "",
    });
  }
  if (isTaskUnresolved(task)) {
    taskFailureReasons(task).forEach((reason) => {
      details.push({ label: reason, meta: "", tone: "bad" });
    });
  }
  if (definition?.allowedPaths?.length) {
    details.push({ label: "Allowed paths", meta: definition.allowedPaths.join(" · ") });
  }
  if (!details.length) {
    details.push({ label: "Waiting for output", meta: task.status.replace(/_/g, " ") });
  }
  return details;
}

function TaskBadge({
  kind,
  ordinal,
}: {
  kind: ReturnType<typeof taskKind>;
  ordinal?: number;
}) {
  if (kind === "done") return <Badge tone="green">{CheckIcon}</Badge>;
  if (kind === "failed") return <Badge tone="red">{XIcon}</Badge>;
  return <SpinnerRing active={kind === "running"}>{ordinal || ""}</SpinnerRing>;
}

function TaskPill({ kind, status }: { kind: ReturnType<typeof taskKind>; status: string }) {
  if (kind === "failed") {
    return (
      <span
        className="inline-flex h-5.5 items-center gap-1.5 rounded-full bg-red-tint px-2 text-[11.5px] font-medium text-red"
        style={{ animation: "fade-in 200ms ease-out both" }}
      >
        Failed
        <span className="flex" style={{ animation: "spin 1.2s linear infinite" }}>
          {RetryIcon}
        </span>
      </span>
    );
  }
  if (kind === "done") {
    return (
      <span className="inline-flex h-5.5 items-center rounded-full bg-green-tint px-2 text-[11.5px] font-medium text-green">
        Completed
      </span>
    );
  }
  if (status.includes("approval")) {
    return (
      <span className="inline-flex h-5.5 items-center rounded-full bg-orange-tint px-2 text-[11.5px] font-medium text-orange">
        Approval
      </span>
    );
  }
  if (kind === "running") {
    return (
      <span className="inline-flex h-5.5 items-center rounded-full bg-accent-tint px-2 text-[11.5px] font-medium text-accent-ink">
        Running
      </span>
    );
  }
  if (kind === "pending") {
    return (
      <span className="inline-flex h-5.5 items-center rounded-full bg-field px-2 text-[11.5px] font-medium text-ink-2">
        {status.replace(/_/g, " ") || "Pending"}
      </span>
    );
  }
  return null;
}

export function TaskRows({
  detail,
  onSelect,
  variant = "Capsules",
}: {
  detail: RunDetail;
  onSelect: (taskId: string) => void;
  variant?: string;
}) {
  const definitions = new Map(
    (detail.plan?.tasks || []).map((task) => [task.id, task]),
  );
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({});
  const list = variant === "List";
  let ordinal = 0;

  return (
    <div
      className={`flex w-full flex-col ${
        list
          ? "gap-0 self-start overflow-hidden rounded-card bg-surface shadow-card"
          : "gap-5"
      }`}
      aria-label="Agent tasks"
    >
      {(detail.levels || [Object.keys(detail.tasks)]).map((wave, waveIndex) => (
        <section className={list ? "flex flex-col" : "flex flex-col gap-2"} key={waveIndex}>
          <span className="wave-label">Wave {waveIndex + 1}</span>
          {wave.map((taskId, index) => {
            const task: TaskState = detail.tasks[taskId];
            const definition = definitions.get(taskId);
            const kind = taskKind(task.status);
            const amount = taskAmount(task, definition);
            const details = taskDetails(task, definition);
            const autoOpen = isTaskUnresolved(task) || kind === "running";
            const open = manualOpen[taskId] ?? autoOpen;
            ordinal += 1;
            const badgeOrdinal = ordinal;
            return (
              <div
                key={taskId}
                className={`self-stretch overflow-hidden transition-[border-radius,background-color] duration-300 hover:bg-inset ${
                  list ? "border-b border-line last:border-0" : "bg-surface shadow-card"
                }`}
                style={{
                  borderRadius: list ? 0 : open ? 14 : 22,
                  animation: `fade-up 450ms cubic-bezier(0.23,1,0.32,1) ${index * 80}ms both`,
                }}
              >
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => {
                    setManualOpen((current) => ({ ...current, [taskId]: !open }));
                    onSelect(taskId);
                  }}
                  className="flex h-11 w-full items-center gap-2.5 px-2.5 text-left"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center">
                    <TaskBadge kind={kind} ordinal={badgeOrdinal} />
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
                    {taskId}
                  </span>
                  {amount && (
                    <span className="text-[12.5px] text-ink-2 tabular-nums">{amount}</span>
                  )}
                  <TaskPill kind={kind} status={task.status} />
                  <span
                    aria-hidden="true"
                    className="-ml-2 flex size-7 shrink-0 items-center justify-center rounded-full text-ink-3"
                  >
                    <svg
                      width="15"
                      height="15"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="transition-transform duration-300"
                      style={{ transform: open ? "rotate(180deg)" : "rotate(0)" }}
                    >
                      <path d="M6 9l6 6 6-6" />
                    </svg>
                  </span>
                </button>
                <div
                  className="grid transition-[grid-template-rows,opacity] duration-300"
                  style={{
                    gridTemplateRows: open ? "1fr" : "0fr",
                    opacity: open ? 1 : 0,
                    transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)",
                  }}
                >
                  <div className="overflow-hidden">
                    <div className="mb-2.5 grid grid-cols-[24px_1fr] gap-2.5 px-2.5">
                      <span aria-hidden className="mx-auto h-full w-px bg-line" />
                      <div className="flex flex-col gap-1.5">
                        {details.map((entry, detailIndex) => (
                          <div
                            key={`${entry.label}-${detailIndex}`}
                            className={`flex items-center justify-between ${entry.tone === "bad" ? "task-reason" : ""}`}
                            style={
                              open
                                ? {
                                    animation: `fade-up 300ms cubic-bezier(0.23,1,0.32,1) ${120 + detailIndex * 100}ms both`,
                                  }
                                : undefined
                            }
                          >
                            <span className={`text-[12px] ${entry.tone === "bad" ? "text-red" : "text-ink-2"}`}>
                              {entry.label}
                            </span>
                            {entry.meta && (
                              <span className="font-mono text-[11.5px] text-ink-3 tabular-nums">
                                {entry.meta}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </section>
      ))}
    </div>
  );
}

export function PlanReview({
  plan,
  levels,
  planPrompt,
  executionDigest,
  taskStates,
  artifacts,
  selectedTaskId,
  onSelectTask,
  onDecideTask,
  title = "Proposed plan",
}: {
  plan: PlanPayload | null | undefined;
  levels?: string[][];
  planPrompt?: PlanPrompt | null;
  executionDigest?: string;
  taskStates?: Record<string, TaskState>;
  artifacts?: RunDetail["artifacts"];
  selectedTaskId?: string | null;
  onSelectTask?: (taskId: string) => void;
  onDecideTask?: (taskId: string, action: "apply" | "reject" | "verify") => void;
  title?: string;
}) {
  if (!plan) return null;
  const tasks = plan.tasks || [];
  const definitions = new Map(tasks.map((task) => [task.id, task]));
  const waves = (levels?.length ? levels : plan.levels) || [tasks.map((task) => task.id)];
  const live = Boolean(taskStates);
  const waiting = Object.values(taskStates || {}).filter((task) => task.status.includes("approval")).length;
  return (
    <section className="plan-review" aria-label="Proposed plan" aria-live={live ? "polite" : undefined}>
      <header>
        <div>
          <p className="kicker">{title}</p>
          <h3>{plan.objective || plan.planId || "Validated plan"}</h3>
          {waiting > 0 && (
            <p className="plan-approval-hint">
              {waiting} {waiting === 1 ? "patch is" : "patches are"} waiting. Apply or reject on the highlighted tasks.
            </p>
          )}
        </div>
        <div className="plan-digests">
          <ToolChip>{tasks.length} tasks</ToolChip>
          <ToolChip>{waves.length} waves</ToolChip>
          {plan.digest && <ToolChip>plan {plan.digest.slice(0, 12)}</ToolChip>}
          {executionDigest && <ToolChip>exec {executionDigest.slice(0, 12)}</ToolChip>}
        </div>
      </header>
      {waves.map((wave, index) => (
        <section className="plan-wave" key={index}>
          <span className="wave-label">Wave {index + 1}</span>
          {wave.map((taskId) => (
            <PlanTaskCard
              task={definitions.get(taskId)}
              taskId={taskId}
              live={taskStates?.[taskId]}
              artifact={artifacts?.[taskId]}
              selected={selectedTaskId === taskId}
              onSelect={onSelectTask}
              onDecide={onDecideTask}
              key={taskId}
            />
          ))}
        </section>
      ))}
      {planPrompt && !live && (
        <details className="plan-task">
          <summary>
            <span className="task-copy">
              <strong>Planning prompt</strong>
              <small>request sent to the frontier commander</small>
            </span>
            <code>sha256:{planPrompt.sha256.slice(0, 12)}</code>
          </summary>
          <div className="plan-task-body">
            <pre><code>{planPrompt.prompt}</code></pre>
          </div>
        </details>
      )}
    </section>
  );
}

function PlanTaskCard({
  task,
  taskId,
  live,
  artifact,
  selected,
  onSelect,
  onDecide,
}: {
  task?: PlanTask;
  taskId: string;
  live?: TaskState;
  artifact?: NonNullable<RunDetail["artifacts"]>[string];
  selected?: boolean;
  onSelect?: (taskId: string) => void;
  onDecide?: (taskId: string, action: "apply" | "reject" | "verify") => void;
}) {
  const dependsOn = task?.dependsOn || [];
  const status = live?.status;
  const kind = status ? taskKind(status) : null;
  const executing = kind === "running";
  const details = live ? taskDetails(live, task) : [];
  const actions = artifact?.actions;
  return (
    <details
      className={`plan-task${executing ? " is-executing" : ""}${selected ? " is-selected" : ""}`}
      open={executing || selected || Boolean(actions?.apply) || undefined}
    >
      <summary onClick={() => onSelect?.(taskId)}>
        {kind && (
          <span className="flex size-6 shrink-0 items-center justify-center">
            <TaskBadge kind={kind} />
          </span>
        )}
        <span className="task-copy">
          <strong>{taskId}</strong>
          <small>
            {task?.role || "agent task"}
            {dependsOn.length ? ` · after ${dependsOn.join(", ")}` : ""}
          </small>
        </span>
        {status && kind ? <TaskPill kind={kind} status={status} /> : <ToolChip>{task?.artifactType || "report"}</ToolChip>}
        {actions?.apply && (
          <span className="plan-task-actions">
            {actions.reject && (
              <button
                type="button"
                className="button quiet"
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  onDecide?.(taskId, "reject");
                }}
              >
                Reject
              </button>
            )}
            <button
              type="button"
              className="button primary"
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onDecide?.(taskId, actions.verify ? "verify" : "apply");
              }}
            >
              {actions.verify ? "Verify" : "Apply"}
            </button>
          </span>
        )}
      </summary>
      <div className="plan-task-body">
        {!!task?.allowedPaths?.length && (
          <p className="plan-meta"><span>Allowed paths</span><code>{task.allowedPaths.join(" · ")}</code></p>
        )}
        {!!task?.verification?.length && (
          <p className="plan-meta"><span>Verification</span><code>{task.verification.join(" · ")}</code></p>
        )}
        {task?.interfaceContract && (
          <p className="plan-meta"><span>Interface</span><code>{task.interfaceContract}</code></p>
        )}
        {details.filter((entry) => entry.tone === "bad" || entry.label.startsWith("Gate")).map((entry) => (
          <p className="plan-meta" key={`${entry.label}-${entry.meta}`}>
            <span>{entry.tone === "bad" ? "Blocked" : "Gate"}</span>
            <code>{entry.label}{entry.meta ? ` ${entry.meta}` : ""}</code>
          </p>
        ))}
        {!live && <pre><code>{task?.prompt || "No prompt was recorded for this task."}</code></pre>}
      </div>
    </details>
  );
}

export function TaskInspector({
  taskId,
  task,
  artifact,
  onRefresh,
  onDecide,
}: {
  taskId: string | null;
  task?: TaskState;
  artifact?: NonNullable<RunDetail["artifacts"]>[string];
  onRefresh?: (taskId: string) => void;
  onDecide?: (taskId: string, action: "apply" | "reject" | "verify") => void;
}) {
  const output = task?.normalizedOutput || task?.output || "";
  const payload = artifact?.payload || "";
  const actions = artifact?.actions;
  const canAct = Boolean(taskId && (actions?.apply || actions?.reject || actions?.verify));
  return (
    <aside className="task-inspector" aria-label="Task detail">
      <header>
        <span>
          <p className="kicker">Task detail</p>
          <h2>{taskId || "No task selected"}</h2>
        </span>
        {taskId && (
          <button type="button" onClick={() => onRefresh?.(taskId)}>
            Refresh
          </button>
        )}
      </header>
      <div className="task-inspector-body">
        {!taskId && <p className="task-inspector-empty">Select a task from the plan.</p>}
        {taskId && task?.status && (
          <p className="plan-meta">
            <span>Status</span>
            <code>{task.status.replace(/_/g, " ")}</code>
          </p>
        )}
        {taskId && payload && (
          <section>
            <p className="kicker">Artifact</p>
            <pre><code>{payload}</code></pre>
          </section>
        )}
        {taskId && output && output !== payload && (
          <section>
            <p className="kicker">Output</p>
            <pre><code>{output}</code></pre>
          </section>
        )}
        {taskId && !payload && !output && (
          <p className="task-inspector-empty">No output yet.</p>
        )}
      </div>
      {canAct && taskId && (
        <footer className="task-inspector-footer">
          {actions?.reject && (
            <button type="button" className="button quiet" onClick={() => onDecide?.(taskId, "reject")}>
              Reject
            </button>
          )}
          <button
            type="button"
            className="button primary"
            onClick={() => onDecide?.(taskId, actions?.verify ? "verify" : "apply")}
          >
            {actions?.verify ? "Verify" : "Apply"}
          </button>
        </footer>
      )}
    </aside>
  );
}

export function ApprovalCard({
  title,
  detail,
  primaryLabel,
  onPrimary,
  onReject,
}: {
  title: string;
  detail: string;
  primaryLabel: string;
  onPrimary: () => void;
  onReject?: () => void;
}) {
  return (
    <section className="approval-card">
      <div className="approval-icon">?</div>
      <div>
        <p className="kicker">Human approval</p>
        <h3>{title}</h3>
        <p>{detail}</p>
      </div>
      <div className="approval-actions">
        {onReject && (
          <button className="button quiet" onClick={onReject}>
            Reject
          </button>
        )}
        <button className="button primary" onClick={onPrimary}>
          {primaryLabel}
        </button>
      </div>
    </section>
  );
}

export function RecommendationCard({
  approvalMode,
  workspaceTarget,
  onApprovalMode,
  onWorkspaceTarget,
}: {
  approvalMode: "supervised" | "yolo";
  workspaceTarget: "worktree" | "checkout";
  onApprovalMode: (value: "supervised" | "yolo") => void;
  onWorkspaceTarget: (value: "worktree" | "checkout") => void;
}) {
  return (
    <section className="recommendation-card">
      <div>
        <p className="kicker">Recommended execution</p>
        <h3>Supervised · isolated worktree</h3>
        <p>Review each digest-bound artifact before it changes the session branch.</p>
      </div>
      <div className="choice-row" aria-label="Artifact decisions">
        {(["supervised", "yolo"] as const).map((value) => (
          <button
            className={approvalMode === value ? "active" : ""}
            onClick={() => onApprovalMode(value)}
            key={value}
          >
            {value}
          </button>
        ))}
      </div>
      <div className="choice-row" aria-label="Execution target">
        {(["worktree", "checkout"] as const).map((value) => (
          <button
            className={workspaceTarget === value ? "active" : ""}
            onClick={() => onWorkspaceTarget(value)}
            disabled={value === "checkout" && approvalMode !== "yolo"}
            key={value}
          >
            {value}
          </button>
        ))}
      </div>
      {workspaceTarget === "checkout" && (
        <ToolChip tone="warn">Checkout requires a clean repository</ToolChip>
      )}
    </section>
  );
}

export function ContextCards({ payload }: { payload: AttemptPayload | null }) {
  if (!payload?.attempts.length) {
    return <div className="empty-card">No rendered prompts were recorded.</div>;
  }
  return (
    <div className="context-list">
      {payload.attempts.map((attempt) => (
        <details className="context-card" key={`${attempt.kind}-${attempt.attempt}`}>
          <summary>
            <span>
              <strong>{attempt.phase}</strong>
              <small>{attempt.kind} attempt {attempt.attempt}</small>
            </span>
            <code>sha256:{attempt.promptSha256.slice(0, 12)}</code>
          </summary>
          <pre><code>{attempt.prompt}</code></pre>
        </details>
      ))}
    </div>
  );
}

export function DiffReview({
  diff,
  split,
}: {
  diff: DiffPayload | null;
  split: boolean;
}) {
  if (!diff?.files.length) {
    return <div className="empty-card">No code changes were recorded.</div>;
  }
  return (
    <div className={`diff-review ${split ? "split" : "unified"}`}>
      {diff.files.map((file) => (
        <section className="diff-file" key={`${file.oldPath}:${file.newPath}`}>
          <header>
            <code>{file.newPath}</code>
            <span>
              <b className="additions">+{file.additions}</b>
              <b className="deletions">−{file.deletions}</b>
            </span>
          </header>
          {file.hunks.map((hunk, hunkIndex) => (
            <div className="diff-hunk" key={hunkIndex}>
              <div className="hunk-header">{hunk.header}</div>
              {split ? (
                <SplitLines lines={hunk.lines} />
              ) : (
                hunk.lines.map((line, index) => (
                  <DiffRow line={line} key={index} />
                ))
              )}
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

function SplitLines({ lines }: { lines: DiffPayload["files"][number]["hunks"][number]["lines"] }) {
  const rows: Array<{ left?: (typeof lines)[number]; right?: (typeof lines)[number] }> = [];
  let pendingDeletes: typeof lines = [];
  for (const line of lines) {
    if (line.type === "delete") {
      pendingDeletes.push(line);
    } else if (line.type === "add" && pendingDeletes.length) {
      rows.push({ left: pendingDeletes.shift(), right: line });
    } else {
      while (pendingDeletes.length) rows.push({ left: pendingDeletes.shift() });
      rows.push(line.type === "add" ? { right: line } : { left: line, right: line });
    }
  }
  while (pendingDeletes.length) rows.push({ left: pendingDeletes.shift() });
  return (
    <div className="split-lines">
      {rows.map((row, index) => (
        <div className="split-row" key={index}>
          {row.left ? <DiffRow line={row.left} /> : <span className="diff-spacer" />}
          {row.right ? <DiffRow line={row.right} /> : <span className="diff-spacer" />}
        </div>
      ))}
    </div>
  );
}

function DiffRow({
  line,
}: {
  line: DiffPayload["files"][number]["hunks"][number]["lines"][number];
}) {
  const marker = line.type === "add" ? "+" : line.type === "delete" ? "−" : " ";
  return (
    <div className={`diff-row ${line.type}`}>
      <span className="line-number">{line.oldLine ?? ""}</span>
      <span className="line-number">{line.newLine ?? ""}</span>
      <span className="marker">{marker}</span>
      <code>{line.text}</code>
    </div>
  );
}

export function RunActions({
  detail,
  onAction,
}: {
  detail: RunDetail;
  onAction: (action: "resume" | "retry" | "cleanup") => void;
}) {
  const actions = detail.actions || {};
  const stuck = Object.entries(detail.tasks).filter(([, task]) => isTaskUnresolved(task));
  if (!actions.resume && !actions.retry && !actions.cleanupWorkspace) return null;
  return (
    <section className="run-actions" aria-label="Run actions">
      <div>
        <p className="kicker">This run stopped early</p>
        <p className="t-sm">
          {stuck.length
            ? `${stuck.map(([id]) => id).join(", ")} did not finish. Read the reason on the task row before choosing.`
            : "Every task finished, but the run did not close cleanly."}
        </p>
      </div>
      <div className="run-action-buttons">
        {actions.resume && (
          <button type="button" onClick={() => onAction("resume")}>
            Resume where it stopped
          </button>
        )}
        {actions.retry && (
          <button type="button" onClick={() => onAction("retry")}>
            Run the plan again
          </button>
        )}
        {actions.cleanupWorkspace && (
          <button type="button" onClick={() => onAction("cleanup")}>
            Remove the worktree
          </button>
        )}
      </div>
    </section>
  );
}

export function readyPlanRequest(request: CommanderRequest): boolean {
  return Boolean(request.planDigest) && request.status !== "launched";
}

export function newlyReadyPlan(
  seen: Set<string> | null,
  requests: CommanderRequest[],
): { seen: Set<string>; discovered?: CommanderRequest } {
  const ready = requests.filter(readyPlanRequest);
  if (seen === null) {
    return { seen: new Set(ready.map((item) => item.requestId)) };
  }
  const discovered = ready.find((item) => !seen.has(item.requestId));
  for (const item of ready) seen.add(item.requestId);
  return { seen, discovered };
}

export function ProjectSidebar({
  projectName,
  requests,
  runs,
  selectedRequestId,
  selectedRunKey,
  onOpenFolder,
  onSelectRequest,
  onSelectReview,
  onNewTask,
  canOpenFolder,
}: {
  projectName: string;
  requests: CommanderRequest[];
  runs: RunSummary[];
  selectedRequestId: string | null;
  selectedRunKey: string | null;
  onOpenFolder?: () => void;
  onSelectRequest: (requestId: string) => void;
  onSelectReview: (run: RunSummary) => void;
  onNewTask: () => void;
  canOpenFolder: boolean;
}) {
  const [query, setQuery] = useState("");
  const [hovered, setHovered] = useState<string | null>(null);
  const [box, setBox] = useState<{ top: number; height: number } | null>(null);
  const navRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const itemRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const visibleRequests = requests.filter((item) =>
    PENDING_REQUEST_STATUSES.includes(item.status)
    && !item.sessionRef
    && matchesQuery(`${item.objective || ""} ${item.requestId} ${item.status}`, query),
  );
  const visibleRuns = runs.filter((run) =>
    matchesQuery(`${run.objective || ""} ${run.planId} ${run.sessionId} ${run.status}`, query),
  );
  const active = selectedRequestId
    ? `plan:${selectedRequestId}`
    : selectedRunKey
      ? `review:${selectedRunKey}`
      : null;
  const initial = projectName.trim().charAt(0).toUpperCase() || "S";

  const itemSignature = [
    ...visibleRequests.map((item) => item.requestId),
    ...visibleRuns.map((run) => `${run.planId}/${run.sessionId}`),
  ].join("|");

  useLayoutEffect(() => {
    const container = navRef.current;
    const target = itemRefs.current[hovered ?? active ?? ""];
    if (!container || !target) {
      setBox((current) => (current ? null : current));
      return;
    }
    const containerRect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const next = {
      top: targetRect.top - containerRect.top + container.scrollTop,
      height: targetRect.height,
    };
    setBox((current) =>
      current && current.top === next.top && current.height === next.height ? current : next,
    );
  }, [hovered, active, itemSignature]);

  useLayoutEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
      event.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <nav className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Opened project">
      <button
        type="button"
        onClick={canOpenFolder ? onOpenFolder : undefined}
        className={`mb-2 flex w-full min-w-0 items-center gap-2.5 rounded-control p-1.5 text-left
          transition-[background-color,transform] duration-100 ${
            canOpenFolder ? "hover:bg-hover active:scale-[0.96]" : "cursor-default"
          }`}
      >
        <span
          className="flex size-8 shrink-0 items-center justify-center rounded-[9px] text-[13px] font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.28)]"
          style={{ background: "linear-gradient(155deg,#5aa2ff,#1f3fb0)" }}
        >
          {initial}
        </span>
        <span className="min-w-0 flex-1 overflow-hidden">
          <span className="sidebar-label block truncate text-[13px] font-medium leading-tight text-ink">{projectName}</span>
          <span className="sidebar-label block truncate text-[11px] leading-tight text-ink-3">Local workspace</span>
        </span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" className="sidebar-label shrink-0">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      <label className="sidebar-search mb-1 flex h-8 min-w-0 items-center gap-2 rounded-control bg-inset px-2.5 shadow-hairline">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round" className="shrink-0">
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.3-4.3" />
        </svg>
        <input
          ref={searchRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Quick search"
          className="min-w-0 flex-1 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
        />
        <kbd className="flex size-[18px] shrink-0 items-center justify-center rounded-[5px] bg-surface text-[10px] text-ink-3 shadow-hairline">
          /
        </kbd>
      </label>

      <button
        type="button"
        onClick={onNewTask}
        className="mb-2 flex w-full min-w-0 items-center gap-2 rounded-control px-2 py-1.5 text-[13px]
          font-medium text-accent transition-[background-color,transform] duration-100 hover:bg-accent-tint active:scale-[0.96]"
      >
        <span className="sidebar-label min-w-0 flex-1 truncate text-left">New task</span>
        <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-accent text-white">
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </span>
      </button>

      <div
        ref={navRef}
        onMouseLeave={() => setHovered(null)}
        className="relative flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-auto"
      >
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-0 rounded-[7px] bg-hover"
          style={{
            top: box?.top ?? 0,
            height: box?.height ?? 0,
            opacity: box ? 1 : 0,
            transition:
              "top 220ms cubic-bezier(0.23,1,0.32,1), height 220ms cubic-bezier(0.23,1,0.32,1), opacity 150ms ease",
          }}
        />
        <SidebarSection title="Plans">
          {visibleRequests.length === 0 && <p className="sidebar-label px-2 py-1 text-[12px] text-ink-3">No plans yet</p>}
          {visibleRequests.map((item) => {
            const key = `plan:${item.requestId}`;
            const isActive = selectedRequestId === item.requestId;
            return (
              <SidebarRow
                key={key}
                itemKey={key}
                itemRefs={itemRefs}
                icon="plan"
                label={item.objective || item.requestId}
                badge={shortStatus(item.status)}
                isActive={isActive}
                onHover={setHovered}
                onClick={() => onSelectRequest(item.requestId)}
              />
            );
          })}
        </SidebarSection>
        <SidebarSection title="Reviews">
          {visibleRuns.length === 0 && <p className="sidebar-label px-2 py-1 text-[12px] text-ink-3">No reviews yet</p>}
          {visibleRuns.map((run) => {
            const runKey = `${run.planId}/${run.sessionId}`;
            const key = `review:${runKey}`;
            const isActive = selectedRunKey === runKey;
            return (
              <SidebarRow
                key={key}
                itemKey={key}
                itemRefs={itemRefs}
                icon="review"
                label={run.objective || run.planId}
                badge={shortStatus(run.status)}
                isActive={isActive}
                onHover={setHovered}
                onClick={() => onSelectReview(run)}
              />
            );
          })}
        </SidebarSection>
      </div>
    </nav>
  );
}

function shortStatus(status: string) {
  return status.replace(/_/g, " ");
}

function SidebarSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="sidebar-label px-2 pb-1 pt-1 text-[10.5px] font-medium uppercase tracking-[0.08em] text-ink-3">
        {title}
      </div>
      <div className="flex min-w-0 flex-col gap-px">{children}</div>
    </div>
  );
}

function SidebarRow({
  itemKey,
  itemRefs,
  icon,
  label,
  badge,
  isActive,
  onHover,
  onClick,
}: {
  itemKey: string;
  itemRefs: MutableRefObject<Record<string, HTMLButtonElement | null>>;
  icon: "plan" | "review";
  label: string;
  badge: string;
  isActive: boolean;
  onHover: (key: string | null) => void;
  onClick: () => void;
}) {
  return (
    <button
      ref={(el) => {
        itemRefs.current[itemKey] = el;
      }}
      type="button"
      onMouseEnter={() => onHover(itemKey)}
      onFocus={() => onHover(itemKey)}
      onBlur={() => onHover(null)}
      onClick={onClick}
      aria-current={isActive ? "page" : undefined}
      className="group relative z-10 flex w-full min-w-0 items-center gap-2 rounded-[7px] px-2 py-1.5 text-left
        transition-[color,transform] duration-150 active:scale-[0.96]"
    >
      <span className={`shrink-0 ${isActive ? "text-ink" : "text-ink-3"}`}>
        <SidebarIcon kind={icon} />
      </span>
      <span
        title={label}
        className={`sidebar-label min-w-0 flex-1 truncate text-[13px] transition-colors duration-150
          ${isActive ? "font-semibold text-ink" : "font-medium text-ink-2"}`}
      >
        {label}
      </span>
      <span
        title={badge}
        className={`sidebar-label max-w-[4.75rem] shrink-0 truncate rounded-full px-1.5 py-px text-[10.5px] font-semibold tabular-nums ${
          isActive ? "bg-surface text-ink-2 shadow-hairline" : "bg-accent-tint text-accent-ink"
        }`}
      >
        {badge}
      </span>
    </button>
  );
}

export const DEFAULT_TERMINAL_SPLIT = 0.5;
export const MIN_TERMINAL_SPLIT = 0.25;
export const MAX_TERMINAL_SPLIT = 0.75;
export const COMMANDER_SKILL = "mlx-swarm-commander";
export const MAP_SKILL = "mlx-swarm-skill-map";
export const PROJECT_CLAUDE_INSTALL =
  "mlx-swarm skill install --host claude --skills-dir .claude/skills";

const SKILL_HOSTS = [
  {
    id: "claude",
    label: "Claude Code",
    installCommand: "mlx-swarm skill install --host claude",
    detail: "Personal Claude skills directory (respects CLAUDE_CONFIG_DIR).",
  },
  {
    id: "codex",
    label: "Codex",
    installCommand: "mlx-swarm skill install --host codex",
    detail: "Personal Codex skills directory (respects CODEX_HOME).",
  },
] as const;

const DEFAULT_SKILLS: SkillStatus[] = [
  {
    skillName: MAP_SKILL,
    hosts: [
      { id: "claude", installed: false, installCommand: SKILL_HOSTS[0].installCommand, invoke: "/mlx-swarm-skill-map" },
      { id: "codex", installed: false, installCommand: SKILL_HOSTS[1].installCommand, invoke: "$mlx-swarm-skill-map" },
    ],
    projectClaudeCommand: PROJECT_CLAUDE_INSTALL,
  },
  {
    skillName: COMMANDER_SKILL,
    hosts: [
      { id: "claude", installed: false, installCommand: SKILL_HOSTS[0].installCommand, invoke: "/mlx-swarm-commander" },
      { id: "codex", installed: false, installCommand: SKILL_HOSTS[1].installCommand, invoke: "$mlx-swarm-commander" },
    ],
    projectClaudeCommand: PROJECT_CLAUDE_INSTALL,
  },
];

export function clampTerminalSplit(value: number) {
  if (!Number.isFinite(value)) return DEFAULT_TERMINAL_SPLIT;
  return Math.min(MAX_TERMINAL_SPLIT, Math.max(MIN_TERMINAL_SPLIT, value));
}

export function layoutSkillMap(
  nodes: CodebaseMapNode[],
): Record<string, { x: number; y: number }> {
  const children = new Map<string, CodebaseMapNode[]>();
  for (const node of nodes) {
    if (!node.parentId) continue;
    const list = children.get(node.parentId) || [];
    list.push(node);
    children.set(node.parentId, list);
  }
  const positions: Record<string, { x: number; y: number }> = {};
  const gap = 28;
  const dx = 168;
  let leafY = 24;
  const visit = (node: CodebaseMapNode, depth: number) => {
    const kids = children.get(node.id) || [];
    if (!kids.length) {
      positions[node.id] = { x: 32 + depth * dx, y: leafY };
      leafY += gap;
      return positions[node.id];
    }
    const placed = kids.map((child) => visit(child, depth + 1));
    const y = (placed[0].y + placed[placed.length - 1].y) / 2;
    positions[node.id] = { x: 32 + depth * dx, y };
    return positions[node.id];
  };
  const roots = nodes.filter((node) => !node.parentId);
  for (const root of roots.length ? roots : nodes.slice(0, 1)) visit(root, 0);
  return positions;
}

export function visibleSkills(skills?: SkillStatus[], skill?: SkillStatus): SkillStatus[] {
  const live = new Map<string, SkillStatus>();
  if (skill?.skillName) live.set(skill.skillName, skill);
  for (const item of skills || []) {
    if (item?.skillName) live.set(item.skillName, item);
  }
  return DEFAULT_SKILLS.map((defaults) => live.get(defaults.skillName) || defaults);
}

export function SkillGuide({
  skill,
  skills,
  configPath,
}: {
  skill?: SkillStatus;
  skills?: SkillStatus[];
  configPath?: string;
}) {
  const items = visibleSkills(skills, skill).map((item) => {
    const hosts = SKILL_HOSTS.map((host) => {
      const status = item.hosts?.find((entry) => entry.id === host.id);
      const fallback = DEFAULT_SKILLS.find((entry) => entry.skillName === item.skillName)
        ?.hosts.find((entry) => entry.id === host.id);
      return {
        ...host,
        installed: Boolean(status?.installed),
        installCommand: status?.installCommand || host.installCommand,
        invoke: status?.invoke || fallback?.invoke || `/${item.skillName}`,
      };
    });
    return {
      ...item,
      hosts,
      projectClaudeCommand: item.projectClaudeCommand || PROJECT_CLAUDE_INSTALL,
    };
  });
  const configFlag = configPath ? `mlx-swarm --config ${configPath}` : "mlx-swarm --config CONFIG";

  return (
    <section className="surface new-task-surface min-w-0">
      <header className="surface-header">
        <div className="min-w-0">
          <p className="kicker">New task</p>
          <h1>Command Swarm from the terminal</h1>
        </div>
        <ToolChip>{MAP_SKILL}</ToolChip>
      </header>
      <p className="new-task-lead">
        This app never calls Claude or Codex. Install the skills below, then run a frontier host
        in the project terminal. Invoke <strong>/mlx-swarm-skill-map</strong> to see the graph,
        then <strong>{COMMANDER_SKILL}</strong> to plan. Simple one-file changes should be edited
        directly in the host, not via Swarm.
      </p>
      <div className="skill-grid skill-grid-compact">
        {items.map((item) => (
          <article className="skill-card min-w-0" key={item.skillName} data-skill={item.skillName}>
            <header className="skill-block-header">
              <h2 className="truncate">{item.skillName}</h2>
              <ToolChip>{item.skillName === MAP_SKILL ? "Skill map" : "Commander"}</ToolChip>
            </header>
            {item.hosts.map((host) => (
              <div className="skill-host-row" key={`${item.skillName}-${host.id}`}>
                <header>
                  <h3 className="truncate">{host.label}</h3>
                  <ToolChip tone={host.installed ? "good" : "neutral"}>
                    {host.installed ? "Installed" : "Install"}
                  </ToolChip>
                </header>
                <p>Invoke <code>{host.invoke}</code>.</p>
                <code className="skill-command truncate" title={host.installCommand}>{host.installCommand}</code>
              </div>
            ))}
            <p>Optional project install:</p>
            <code className="skill-command truncate" title={item.projectClaudeCommand}>{item.projectClaudeCommand}</code>
          </article>
        ))}
      </div>
      <article className="skill-card min-w-0">
        <p className="kicker">How the workflow works</p>
        <ol className="workflow-list">
          <li>Open this page. The terminal cwd is the opened project.</li>
          <li>Install the skills if the cards above say Install.</li>
          <li>Click a node on the skill map to point Swarm at that path, or invoke <code>/mlx-swarm-skill-map</code>.</li>
          <li>In the terminal run <code>claude</code> or <code>codex</code> and invoke the commander skill with the objective.</li>
          <li>
            The skills use the <code>mlx-swarm</code> CLI (<code>map</code>, <code>commander claim-plan</code> / <code>import-plan</code>).
            Do not reproduce persistence in ad hoc scripts.
            {configPath ? <> Pass <code className="skill-inline truncate" title={configFlag}>{configFlag}</code>.</> : null}
          </li>
          <li>
            When a plan is imported, it appears under <strong>Plans</strong>.
            {" "}<strong>Continue</strong> in the cockpit is digest-bound approve-and-run.
            The skill must not approve for the operator.
          </li>
          <li>
            Local MLX workers execute. After completion, use the commander skill to review
            {" "}(<code>claim-review</code> / <code>import-review</code>).
          </li>
        </ol>
      </article>
    </section>
  );
}

export function SkillMapGraph({
  map,
  error,
  focusPaths = [],
  onFocus,
  onOpenFolder,
  canOpenFolder,
}: {
  map?: CodebaseMap | null;
  error?: string;
  focusPaths?: string[];
  onFocus?: (paths: string[]) => void;
  onOpenFolder?: () => void;
  canOpenFolder?: boolean;
}) {
  const [localFocus, setLocalFocus] = useState(focusPaths);
  const selected = localFocus[0] || "";
  const positions = useMemo(() => layoutSkillMap(map?.nodes || []), [map]);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const drag = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null);

  useEffect(() => {
    setLocalFocus(focusPaths);
  }, [focusPaths]);

  const width = useMemo(() => {
    const xs = Object.values(positions).map((point) => point.x);
    return Math.max(640, (xs.length ? Math.max(...xs) : 0) + 220);
  }, [positions]);
  const height = useMemo(() => {
    const ys = Object.values(positions).map((point) => point.y);
    return Math.max(280, (ys.length ? Math.max(...ys) : 0) + 48);
  }, [positions]);

  async function copyPath(path: string) {
    try {
      await navigator.clipboard.writeText(path);
    } catch {
      /* clipboard may be unavailable in tests */
    }
  }

  function selectNode(path: string) {
    setLocalFocus([path]);
    onFocus?.([path]);
    void copyPath(path);
  }

  return (
    <section className="surface new-task-surface skill-map-surface min-w-0">
      <header className="surface-header">
        <div className="min-w-0">
          <p className="kicker">Skill map</p>
          <h2>Point Swarm at the codebase</h2>
        </div>
        {map?.truncated ? <ToolChip>Truncated</ToolChip> : null}
      </header>
      <p className="new-task-lead">
        Click a folder or file to highlight it. The path is copied and stored as
        {" "}<code>focusPaths</code> for {MAP_SKILL}. It does not open a different folder or create a plan.
      </p>
      {selected ? (
        <p className="skill-map-path">
          <span className="kicker">Focus</span>
          <code title={selected}>{selected}</code>
          <button type="button" className="ghost-button" onClick={() => void copyPath(selected)}>Copy path</button>
        </p>
      ) : null}
      {error ? <p className="skill-map-empty">{error}</p> : null}
      {!error && !map ? <p className="skill-map-empty">Loading skill map…</p> : null}
      {!error && map && !map.nodes.length ? (
        <div className="skill-map-empty">
          <p>No workspace graph yet.</p>
          {canOpenFolder ? (
            <button type="button" className="ghost-button" onClick={onOpenFolder}>Open folder</button>
          ) : null}
        </div>
      ) : null}
      {map && map.nodes.length > 0 ? (
        <div
          className="skill-map-viewport"
          onWheel={(event) => {
            event.preventDefault();
            setZoom((value) => Math.min(2.2, Math.max(0.45, value + (event.deltaY > 0 ? -0.08 : 0.08))));
          }}
          onMouseDown={(event) => {
            if ((event.target as HTMLElement).closest("[data-node]")) return;
            drag.current = { x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y };
          }}
          onMouseMove={(event) => {
            if (!drag.current) return;
            setPan({
              x: drag.current.panX + event.clientX - drag.current.x,
              y: drag.current.panY + event.clientY - drag.current.y,
            });
          }}
          onMouseUp={() => {
            drag.current = null;
          }}
          onMouseLeave={() => {
            drag.current = null;
          }}
        >
          <svg
            className="skill-map-svg"
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label="Codebase skill map"
          >
            <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
              {(map.edges || []).map((edge) => {
                const from = positions[edge.source];
                const to = positions[edge.target];
                if (!from || !to) return null;
                return (
                  <path
                    key={`${edge.source}->${edge.target}`}
                    className="skill-map-edge"
                    d={`M ${from.x + 10} ${from.y} C ${from.x + 70} ${from.y}, ${to.x - 40} ${to.y}, ${to.x - 10} ${to.y}`}
                  />
                );
              })}
              {(map.nodes || []).map((node) => {
                const point = positions[node.id];
                if (!point) return null;
                const active = selected === node.path;
                return (
                  <g
                    key={node.id}
                    data-node={node.path}
                    transform={`translate(${point.x} ${point.y})`}
                    className={`skill-map-node${active ? " is-active" : ""}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      selectNode(node.path);
                    }}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        selectNode(node.path);
                      }
                    }}
                  >
                    <circle r="7" />
                    <text x="14" y="4">{node.name}{node.kind !== "file" && node.fileCount ? ` (${node.fileCount})` : ""}</text>
                  </g>
                );
              })}
            </g>
          </svg>
        </div>
      ) : null}
    </section>
  );
}

export function NewTask({
  skill,
  skills,
  configPath,
  desktop,
  terminalVisible,
  terminalSplit,
  onSplitChange,
  onSplitCommit,
  terminal,
  map: mapProp,
  focusPaths = [],
  onFocusPaths,
  onOpenFolder,
  canOpenFolder,
}: {
  skill?: SkillStatus;
  skills?: SkillStatus[];
  configPath?: string;
  desktop: boolean;
  terminalVisible: boolean;
  terminalSplit: number;
  onSplitChange: (value: number) => void;
  onSplitCommit: (value: number) => void;
  terminal?: ReactNode;
  map?: CodebaseMap | null;
  focusPaths?: string[];
  onFocusPaths?: (paths: string[]) => void;
  onOpenFolder?: () => void;
  canOpenFolder?: boolean;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const latestSplit = useRef(terminalSplit);
  const onSplitChangeRef = useRef(onSplitChange);
  const onSplitCommitRef = useRef(onSplitCommit);
  const showTerminalPane = !desktop || terminalVisible;
  const [fetchedMap, setFetchedMap] = useState<CodebaseMap | null>(null);
  const [mapError, setMapError] = useState("");
  latestSplit.current = terminalSplit;
  onSplitChangeRef.current = onSplitChange;
  onSplitCommitRef.current = onSplitCommit;

  useEffect(() => {
    const onMove = (event: MouseEvent) => {
      if (!dragging.current || !rootRef.current) return;
      event.preventDefault();
      const rect = rootRef.current.getBoundingClientRect();
      if (rect.height <= 0) return;
      const next = clampTerminalSplit(1 - (event.clientY - rect.top) / rect.height);
      latestSplit.current = next;
      onSplitChangeRef.current(next);
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.classList.remove("split-dragging");
      onSplitCommitRef.current(latestSplit.current);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.classList.remove("split-dragging");
    };
  }, []);

  useEffect(() => {
    if (mapProp !== undefined) return;
    let cancelled = false;
    fetch("/api/workspace/map", { cache: "no-store" })
      .then(async (response) => {
        const value = await response.json();
        if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
        return value as CodebaseMap;
      })
      .then((value) => {
        if (!cancelled) setFetchedMap(value);
      })
      .catch((reason) => {
        if (!cancelled) setMapError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [mapProp, configPath]);

  const map = mapProp !== undefined ? mapProp : fetchedMap;

  return (
    <div
      ref={rootRef}
      className="new-task-split"
      data-split={String(terminalSplit)}
      data-collapsed={showTerminalPane ? "false" : "true"}
      style={
        showTerminalPane
          ? {
              gridTemplateRows: `minmax(0, ${1 - terminalSplit}fr) 8px minmax(0, ${terminalSplit}fr)`,
            }
          : undefined
      }
    >
      <div className="new-task-guide min-w-0">
        <SkillMapGraph
          map={map}
          error={mapError}
          focusPaths={focusPaths}
          onFocus={onFocusPaths}
          onOpenFolder={onOpenFolder}
          canOpenFolder={canOpenFolder}
        />
        <SkillGuide skill={skill} skills={skills} configPath={configPath} />
      </div>
      {showTerminalPane && (
        <>
          <button
            type="button"
            className="split-handle"
            aria-label="Resize terminal"
            aria-orientation="horizontal"
            onMouseDown={(event) => {
              event.preventDefault();
              dragging.current = true;
              document.body.classList.add("split-dragging");
            }}
          />
          <div className="new-task-terminal min-w-0">
            {terminal || (
              <div className="terminal-placeholder" role="note">
                <p className="kicker">Project terminal</p>
                <h2>Desktop only</h2>
                <p>
                  The live PTY is available in <code>mlx-swarm app</code>. Browser UI has no PTY.
                  Use your own terminal with cwd in this project, or open the desktop shell.
                </p>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SidebarIcon({ kind }: { kind: "plan" | "review" }) {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      {kind === "plan" ? (
        <g>
          <rect x="4" y="4" width="16" height="16" rx="4.5" />
          <path d="m8.6 12 2.4 2.4L15.6 9.5" />
        </g>
      ) : (
        <g>
          <path d="M20 5H4a1.5 1.5 0 0 0-1.5 1.5V9h19V6.5A1.5 1.5 0 0 0 20 5Z" />
          <path d="M21.5 9v8.5A1.5 1.5 0 0 1 20 19H4a1.5 1.5 0 0 1-1.5-1.5V9M8.5 12.5h7" />
        </g>
      )}
    </svg>
  );
}

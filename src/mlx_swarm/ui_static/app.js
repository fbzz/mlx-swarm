// @lat: [[UI]]
"use strict";

const appState = {
  system: null,
  plans: [],
  invalidPlans: [],
  commanderRequests: [],
  evaluations: [],
  evaluationDetail: null,
  selectedRequest: null,
  requestDetail: null,
  runs: [],
  detail: null,
  selectedRun: null,
  selectedTask: null,
  viewMode: null,
  activeTab: "overview",
  timer: null,
  toastTimer: null,
};
const el = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  refreshAll();
});

function bindEvents() {
  el("commander-form").addEventListener("submit", createCommanderRequest);
  el("commander-select").addEventListener("change", selectCommanderRequest);
  el("copy-plan-command").addEventListener("click", copyPlanCommand);
  el("approve-run-button").addEventListener("click", approveCommanderRun);
  el("launch-form").addEventListener("submit", launchRun);
  el("plan-select").addEventListener("change", selectPlanForPreview);
  el("approval-mode").addEventListener("change", executionChoiceChanged);
  el("workspace-target").addEventListener("change", executionChoiceChanged);
  el("run-search").addEventListener("input", renderRuns);
  el("refresh-button").addEventListener("click", () => refreshAll(true));
  el("resume-button").addEventListener("click", resumeRun);
  el("retry-button").addEventListener("click", retryRun);
  el("review-button").addEventListener("click", copyReviewCommand);
  el("cleanup-workspace-button").addEventListener("click", cleanupWorkspace);
  el("evaluation-select").addEventListener("change", selectEvaluation);
  el("copy-review-command").addEventListener("click", copyReviewCommand);
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
    button.addEventListener("keydown", tabKeydown);
  });
  document.addEventListener("visibilitychange", () => {
    clearPoll();
    if (!document.hidden) refreshAll(false);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function refreshAll(manual = false) {
  try {
    const [system, plansPayload, commanderPayload, runsPayload, evaluationsPayload] = await Promise.all([
      api("/api/status"), api("/api/plans"), api("/api/commander/requests"), api("/api/runs"),
      api("/api/evaluations"),
    ]);
    appState.system = system;
    appState.plans = plansPayload.plans || [];
    appState.invalidPlans = plansPayload.invalid || [];
    appState.commanderRequests = commanderPayload.requests || [];
    appState.runs = runsPayload.runs || [];
    appState.evaluations = evaluationsPayload.evaluations || [];
    renderSystem();
    renderPlans();
    renderCommanderRequests();
    renderRuns();
    renderEvaluations();

    if (appState.viewMode === "catalog") {
      selectPlanForPreview();
    } else if (appState.viewMode === "commander" && appState.selectedRequest) {
      const exists = appState.commanderRequests.some(
        (request) => request.requestId === appState.selectedRequest);
      if (exists) await loadCommanderRequest(appState.selectedRequest, false);
      else clearSelection();
    } else if (appState.selectedRun) {
      const exists = appState.runs.some((run) => runKey(run) === appState.selectedRun);
      if (exists) await loadRunByKey(appState.selectedRun, false);
      else clearSelection();
    } else if (appState.runs.length) {
      await selectRun(appState.runs[0]);
    } else if (appState.commanderRequests.length) {
      appState.selectedRequest = appState.commanderRequests[0].requestId;
      appState.viewMode = "commander";
      el("commander-select").value = appState.selectedRequest;
      await loadCommanderRequest(appState.selectedRequest, true);
    }
    if (manual) showToast("Cockpit state refreshed.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    schedulePoll();
  }
}

function renderSystem() {
  const node = el("model-status");
  node.replaceChildren();
  const dot = document.createElement("span");
  dot.className = `status-dot ${appState.system?.ready ? "completed" : "failed"}`;
  node.append(dot, document.createTextNode(appState.system?.ready ? "Ready" : "Not ready"));
  node.title = appState.system?.model?.path || appState.system?.model?.error || "";
}

function renderPlans() {
  const select = el("plan-select");
  const previous = select.value;
  select.replaceChildren();
  if (!appState.plans.length) {
    select.add(new Option("No valid plans found", ""));
    select.disabled = true;
    el("launch-button").disabled = true;
  } else {
    select.disabled = false;
    el("launch-button").disabled = false;
    appState.plans.forEach((plan) => {
      select.add(new Option(`${plan.planId} · ${plan.tasks.length} tasks`, plan.planId));
    });
    if (appState.plans.some((plan) => plan.planId === previous)) select.value = previous;
  }
  el("plan-count").textContent = String(appState.plans.length);
  const warning = el("plan-warning");
  if (appState.invalidPlans.length) {
    const first = appState.invalidPlans[0];
    warning.textContent = `${appState.invalidPlans.length} plan file${appState.invalidPlans.length === 1 ? "" : "s"} excluded. ${first.path}: ${first.error}`;
    warning.hidden = false;
  } else {
    warning.hidden = true;
  }
  if (appState.viewMode !== "commander") renderPlanDescription();
}

function renderEvaluations() {
  const select = el("evaluation-select");
  const previous = select.value;
  select.replaceChildren();
  el("evaluation-count").textContent = String(appState.evaluations.length);
  if (!appState.evaluations.length) {
    select.add(new Option("No evaluations yet", ""));
    select.disabled = true;
    renderEvaluationSummary(null);
    return;
  }
  select.disabled = false;
  select.add(new Option("Select an evaluation", ""));
  appState.evaluations.forEach((evaluation) => {
    select.add(new Option(
      `${evaluation.evaluationId} · ${(evaluation.status || "unknown").replaceAll("_", " ")}`,
      evaluation.evaluationId,
    ));
  });
  if (appState.evaluations.some((item) => item.evaluationId === previous)) {
    select.value = previous;
  } else if (appState.evaluationDetail?.evaluation?.evaluationId) {
    select.value = appState.evaluationDetail.evaluation.evaluationId;
  }
}

async function selectEvaluation() {
  const evaluationId = el("evaluation-select").value;
  if (!evaluationId) {
    appState.evaluationDetail = null;
    renderEvaluationSummary(null);
    return;
  }
  try {
    appState.evaluationDetail = await api(
      `/api/evaluations/${encodeURIComponent(evaluationId)}`,
    );
    renderEvaluationSummary(appState.evaluationDetail);
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderEvaluationSummary(detail) {
  const panel = el("evaluation-summary");
  panel.replaceChildren();
  if (!detail) {
    const message = document.createElement("p");
    message.className = "field-hint";
    message.textContent = "Prepare and run a paired study to see measured economics.";
    panel.append(message);
    return;
  }
  const summary = detail.summary;
  const state = document.createElement("span");
  state.className = `state-chip ${summary?.claim?.status === "established" ? "completed" : "pending"}`;
  state.textContent = summary?.claim?.status?.replaceAll("_", " ")
    || detail.evaluation.status.replaceAll("_", " ");
  panel.append(state);
  const copy = document.createElement("p");
  copy.className = "field-hint";
  copy.textContent = summary?.claim?.text
    || `${detail.results.length} immutable arm result${detail.results.length === 1 ? "" : "s"}.`;
  panel.append(copy);
  if (!summary) return;
  const table = document.createElement("table");
  table.className = "economics-mini-table";
  const rows = [
    ["Score", `${summary.frontierAlone.score}/${summary.measuredCases}`, `${summary.mlxSwarm.score}/${summary.measuredCases}`],
    ["Median time", formatDuration(summary.frontierAlone.medianElapsedSeconds), formatDuration(summary.mlxSwarm.medianElapsedSeconds)],
    ["Frontier tokens", formatNumber(summary.frontierAlone.frontierTokens), formatNumber(summary.mlxSwarm.frontierTokens)],
    ["Local tokens", "—", formatNumber(summary.mlxSwarm.localTokens)],
  ];
  const head = document.createElement("tr");
  ["Metric", "Frontier", "Swarm"].forEach((value) => {
    const cell = document.createElement("th");
    cell.textContent = value;
    head.append(cell);
  });
  table.append(head);
  rows.forEach((values) => {
    const row = document.createElement("tr");
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    table.append(row);
  });
  panel.append(table);
}

function renderPlanDescription() {
  const plan = appState.plans.find((item) => item.planId === el("plan-select").value);
  const workspacePlan = plan?.schemaVersion >= 2;
  el("execution-policy-controls").hidden = !workspacePlan;
  if (!workspacePlan) {
    el("approval-mode").value = "supervised";
    el("workspace-target").value = "worktree";
  }
  synchronizeExecutionChoice();
  const execution = selectedExecutionPreview(plan);
  el("plan-description").textContent = plan
    ? `${plan.objective}${workspacePlan
      ? ` · ${executionLabel()} · ${shortSha(execution?.baseSha)}`
      : " · generation only"}`
    : "Select a validated plan from the configured directory.";
  el("launch-button").disabled = !plan || execution?.ready === false;
  const launchLabel = workspacePlan
    ? el("approval-mode").value === "yolo"
      ? "Approve YOLO run"
      : "Approve and launch"
    : "Launch run";
  (el("launch-button").querySelector("span") || el("launch-button")).textContent = launchLabel;
  const policyWarning = el("execution-policy-warning");
  policyWarning.hidden = !workspacePlan;
  policyWarning.textContent = workspacePlan
    ? el("workspace-target").value === "checkout"
      ? "YOLO will commit approved local artifacts directly to the current clean branch. Verification failure pauses with the commit visible."
      : el("approval-mode").value === "yolo"
        ? "YOLO auto-applies digest-bound artifacts inside an isolated worktree, then runs only configured verification profiles."
        : "Supervised mode pauses before every workspace-changing artifact."
    : "";
  if (execution?.error) {
    const warning = el("plan-warning");
    warning.textContent = execution.error;
    warning.hidden = false;
  } else if (!appState.invalidPlans.length) {
    el("plan-warning").hidden = true;
  }
}

function synchronizeExecutionChoice() {
  const mode = el("approval-mode").value;
  const checkout = [...el("workspace-target").options]
    .find((option) => option.value === "checkout");
  if (checkout) checkout.disabled = mode !== "yolo";
  if (mode !== "yolo" && el("workspace-target").value === "checkout") {
    el("workspace-target").value = "worktree";
  }
}

function executionChoiceChanged() {
  synchronizeExecutionChoice();
  if (appState.viewMode === "commander") {
    renderCommanderPreview();
  } else {
    renderPlanDescription();
  }
}

function executionLabel() {
  const mode = el("approval-mode").value === "yolo" ? "YOLO" : "supervised";
  const target = el("workspace-target").value === "checkout"
    ? "main checkout"
    : "isolated worktree";
  return `${mode} · ${target}`;
}

function selectedExecutionPreview(source) {
  if (!source) return null;
  const previews = source.executionPreviews;
  const selected = previews?.[el("approval-mode").value]
    ?.[el("workspace-target").value];
  return selected || source.executionPreview || source.execution || null;
}

function selectPlanForPreview() {
  renderPlanDescription();
  const plan = appState.plans.find(
    (item) => item.planId === el("plan-select").value);
  if (!plan) return;
  appState.selectedRun = null;
  appState.selectedRequest = null;
  appState.detail = null;
  appState.selectedTask = plan.tasks[0]?.id || null;
  appState.viewMode = "catalog";
  appState.requestDetail = {
    request: {
      requestId: "approved plan file",
      status: "plan_ready",
      objective: plan.objective,
      planDigest: plan.digest,
      workspaceRoot: plan.execution?.workspaceRoot
        || appState.system?.workspaceRoot
        || appState.system?.plansDir
        || "",
    },
    plan: {
      ...plan,
      levels: planLevels(plan.tasks),
    },
    executionPreview: plan.execution || null,
    executionPreviews: plan.executionPreviews || null,
    executionError: plan.execution?.error || null,
    validationError: null,
    handoff: {},
  };
  renderRuns();
  renderCommanderPreview();
  renderInspector();
}

function planLevels(tasks) {
  const remaining = new Map(tasks.map((task) => [task.id, task]));
  const completed = new Set();
  const levels = [];
  while (remaining.size) {
    const ready = [...remaining.values()]
      .filter((task) => (task.dependsOn || []).every(
        (dependency) => completed.has(dependency)))
      .map((task) => task.id);
    if (!ready.length) return [[...remaining.keys()]];
    levels.push(ready);
    ready.forEach((taskId) => {
      completed.add(taskId);
      remaining.delete(taskId);
    });
  }
  return levels;
}

function renderCommanderRequests() {
  const select = el("commander-select");
  const previous = appState.selectedRequest || select.value;
  select.replaceChildren();
  if (!appState.commanderRequests.length) {
    select.add(new Option("No requests yet", ""));
    select.disabled = true;
  } else {
    select.disabled = false;
    select.add(new Option("Select a request", ""));
    appState.commanderRequests.forEach((request) => {
      const label = `${request.requestId} · ${request.status.replaceAll("_", " ")}`;
      select.add(new Option(label, request.requestId));
    });
    if (appState.commanderRequests.some((request) => request.requestId === previous)) {
      select.value = previous;
    }
  }
  el("commander-count").textContent = String(appState.commanderRequests.length);
}

async function selectCommanderRequest() {
  const requestId = el("commander-select").value;
  if (!requestId) return;
  appState.selectedRequest = requestId;
  appState.selectedRun = null;
  appState.selectedTask = null;
  appState.viewMode = "commander";
  renderRuns();
  await loadCommanderRequest(requestId, true);
}

async function loadCommanderRequest(requestId, chooseTask) {
  try {
    const detail = await api(`/api/commander/requests/${encodeURIComponent(requestId)}`);
    appState.requestDetail = detail;
    appState.selectedRequest = requestId;
    appState.viewMode = "commander";
    if (chooseTask || !detail.plan?.tasks?.some((task) => task.id === appState.selectedTask)) {
      appState.selectedTask = detail.plan?.tasks?.[0]?.id || null;
    }
    renderCommanderPreview();
    renderInspector();
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderCommanderPreview() {
  const detail = appState.requestDetail;
  if (!detail) return;
  el("empty-state").hidden = true;
  el("run-workspace").hidden = true;
  el("commander-workspace").hidden = false;
  const request = detail.request;
  const plan = detail.plan;
  const revision = detail.revisionInput;
  const workspacePlan = plan?.schemaVersion >= 2;
  const status = request.status || "awaiting_plan";
  const historicalApproval = status === "launched" && request.approval;
  if (historicalApproval) {
    el("approval-mode").value = request.approval.approvalMode || "supervised";
    el("workspace-target").value = request.approval.workspaceTarget || "worktree";
  }
  el("execution-policy-controls").hidden = !workspacePlan || Boolean(historicalApproval);
  if (!workspacePlan) {
    el("approval-mode").value = "supervised";
    el("workspace-target").value = "worktree";
  }
  synchronizeExecutionChoice();
  const catalogPreview = appState.viewMode === "catalog";
  setStateChip(el("commander-state-chip"), status);
  el("commander-request-id").textContent = request.requestId || "";
  el("commander-plan-title").textContent = plan?.planId || "Frontier planning request";
  el("commander-plan-objective").textContent = plan?.objective || request.objective || "";
  el("commander-workspace-root").textContent = (
    revision?.inspectionRoot
    || request.workspaceRoot
    || ""
  );
  el("commander-plan-digest").textContent = request.planDigest || "Awaiting validated frontier plan";
  const execution = selectedExecutionPreview(detail);
  el("commander-execution-digest-wrap").hidden = !execution;
  el("commander-base-wrap").hidden = !execution;
  el("commander-execution-digest").textContent = execution?.executionDigest || "";
  el("commander-base-sha").textContent = execution?.baseSha || "";
  const revisionMeta = revision
    ? ` · revision of ${revision.revisionOf} · ${(revision.carriedTasks || []).length} carried`
    : "";
  el("commander-plan-meta").textContent = plan
    ? `${plan.tasks.length} task${plan.tasks.length === 1 ? "" : "s"} · ${plan.levels.length} wave${plan.levels.length === 1 ? "" : "s"}${revisionMeta}`
    : `No validated DAG yet${revisionMeta}`;
  el("approve-run-button").hidden = catalogPreview || status !== "plan_ready";
  el("approve-run-button").disabled = (
    status !== "plan_ready"
    || execution?.ready === false
    || Boolean(detail.executionError && !execution)
  );
  el("approve-run-button").textContent = (
    el("approval-mode").value === "yolo"
      ? "Approve YOLO run"
      : "Approve and run"
  );

  const handoff = el("commander-handoff");
  handoff.hidden = catalogPreview;
  el("commander-command").textContent = detail.handoff?.planCommand || "";
  el("commander-status-copy").textContent = catalogPreview
    ? "Review the complete contract, then use Approve and launch in the plan rail."
    : commanderStatusCopy(status);

  const error = el("commander-error");
  if (detail.validationError || execution?.error || detail.executionError) {
    error.textContent = detail.validationError?.error
      || execution?.error
      || detail.executionError
      || "The frontier plan failed validation.";
    error.hidden = false;
  } else {
    error.hidden = true;
  }

  renderCommanderDag(detail);
  const displayPlan = plan ? {...plan} : null;
  if (displayPlan) {
    delete displayPlan.source;
    delete displayPlan.digest;
    delete displayPlan.levels;
    delete displayPlan.execution;
  }
  el("commander-plan-json").textContent = displayPlan
    ? JSON.stringify(displayPlan, null, 2)
    : "The validated plan will appear here after the Codex handoff is imported.";
}

function renderCommanderDag(detail) {
  const dag = el("commander-dag");
  dag.replaceChildren();
  const plan = detail.plan;
  if (!plan) {
    dag.append(emptyCopy("Awaiting one validated frontier planning response."));
    return;
  }
  const definitions = new Map(plan.tasks.map((task) => [task.id, task]));
  let nodeIndex = 0;
  plan.levels.forEach((level, levelIndex) => {
    const column = document.createElement("section");
    column.className = "dag-level";
    column.setAttribute("aria-label", `Planned execution wave ${levelIndex + 1}`);
    const label = document.createElement("span");
    label.className = "level-label";
    label.textContent = `Wave ${String(levelIndex + 1).padStart(2, "0")}`;
    column.append(label);
    level.forEach((taskId) => {
      nodeIndex += 1;
      const task = definitions.get(taskId) || {};
      const button = document.createElement("button");
      button.type = "button";
      button.className = `dag-node pending${appState.selectedTask === taskId ? " selected" : ""}`;
      button.setAttribute("aria-pressed", appState.selectedTask === taskId ? "true" : "false");
      button.setAttribute("aria-label", `${taskId}, ${task.role || "general"}, pending approval`);
      button.addEventListener("click", () => {
        appState.selectedTask = taskId;
        renderCommanderDag(appState.requestDetail);
        renderInspector();
      });
      const top = document.createElement("span");
      top.className = "dag-node-top";
      const index = document.createElement("span");
      index.className = "node-index";
      index.textContent = String(nodeIndex).padStart(2, "0");
      const state = document.createElement("span");
      state.className = "node-status pending";
      state.textContent = "planned";
      top.append(index, state);
      const name = document.createElement("strong");
      name.className = "node-name";
      name.textContent = taskId;
      const role = document.createElement("span");
      role.className = "node-role";
      role.textContent = task.role || "general";
      const footer = document.createElement("span");
      footer.className = "node-footer";
      const deps = document.createElement("span");
      const dependencyCount = (task.dependsOn || []).length;
      deps.textContent = dependencyCount
        ? `${dependencyCount} dep${dependencyCount === 1 ? "" : "s"}`
        : "root";
      const repairs = document.createElement("span");
      repairs.textContent = `${task.maxRepairAttempts ?? 0} repair${task.maxRepairAttempts === 1 ? "" : "s"}`;
      footer.append(deps, repairs);
      button.append(top, name, role, footer);
      column.append(button);
    });
    dag.append(column);
  });
}

function commanderStatusCopy(status) {
  const messages = {
    awaiting_plan: "Copy the handoff into Codex. The cockpit will poll for the validated plan.",
    plan_invalid: "The single planning response failed validation. Create a new request to try again.",
    plan_ready: "Preview the complete contract and digest, then approve it to launch local work.",
    launched: "This immutable request has already launched its approved local run.",
  };
  return messages[status] || status.replaceAll("_", " ");
}

function renderRuns() {
  const list = el("run-list");
  const query = el("run-search").value.trim().toLowerCase();
  const runs = appState.runs.filter((run) =>
    `${run.planId || ""} ${run.sessionId || ""} ${run.objective || ""}`.toLowerCase().includes(query));
  list.replaceChildren();
  if (!runs.length) {
    const empty = document.createElement("div");
    empty.className = "run-list-empty";
    empty.textContent = query ? "No runs match this search." : "No runs yet. Launch a plan to begin local work.";
    list.append(empty);
    return;
  }
  runs.forEach((run) => {
    const status = displayStatus(run);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-card ${status}${runKey(run) === appState.selectedRun ? " selected" : ""}`;
    button.setAttribute("aria-label", `${run.planId}, ${status}, ${run.completed} of ${run.total} tasks complete`);
    button.addEventListener("click", () => selectRun(run));

    const accent = document.createElement("span");
    accent.className = "run-accent";
    const main = document.createElement("span");
    main.className = "run-card-main";
    const top = document.createElement("span");
    top.className = "run-card-top";
    const title = document.createElement("span");
    title.className = "run-card-title";
    title.textContent = run.planId || "Unknown plan";
    const state = document.createElement("span");
    state.className = `run-card-status ${status}`;
    state.textContent = status;
    top.append(title, state);
    const meta = document.createElement("span");
    meta.className = "run-card-meta";
    const session = document.createElement("span");
    session.textContent = shortSession(run.sessionId);
    const progress = document.createElement("span");
    progress.textContent = `${run.completed || 0}/${run.total || 0}`;
    const policy = document.createElement("span");
    policy.textContent = run.approvalMode === "yolo"
      ? `YOLO · ${run.workspaceTarget === "checkout" ? "checkout" : "worktree"}`
      : run.workspaceTarget
        ? "supervised"
        : "";
    meta.append(session, progress);
    if (policy.textContent) meta.append(policy);
    main.append(top, meta);
    button.append(accent, main);
    list.append(button);
  });
}

async function selectRun(run) {
  appState.selectedRun = runKey(run);
  appState.selectedRequest = null;
  appState.requestDetail = null;
  appState.selectedTask = null;
  appState.viewMode = "run";
  el("commander-select").value = "";
  renderRuns();
  await loadRunByKey(appState.selectedRun, true);
}

async function loadRunByKey(key, chooseTask) {
  const [planId, sessionId] = splitRunKey(key);
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(planId)}/${encodeURIComponent(sessionId)}`);
    appState.detail = detail;
    if (chooseTask && !appState.selectedTask) appState.selectedTask = firstInterestingTask(detail);
    if (appState.selectedTask && !detail.tasks[appState.selectedTask]) {
      appState.selectedTask = firstInterestingTask(detail);
    }
    updateRunInList(detail.run);
    renderRuns();
    renderRun();
    renderInspector();
  } catch (error) {
    showToast(error.message, true);
  }
}

function updateRunInList(run) {
  const index = appState.runs.findIndex((item) => runKey(item) === runKey(run));
  if (index >= 0) appState.runs[index] = run;
  else appState.runs.unshift(run);
}

function renderRun() {
  const detail = appState.detail;
  if (!detail) {
    clearSelection();
    return;
  }
  el("empty-state").hidden = true;
  el("commander-workspace").hidden = true;
  el("run-workspace").hidden = false;
  appState.viewMode = "run";
  const run = detail.run;
  const status = displayStatus(run);
  setStateChip(el("run-state-chip"), status);
  el("run-session-id").textContent = run.sessionId || "";
  el("dag-title").textContent = run.planId || "Run";
  el("run-objective").textContent = detail.plan?.objective || run.objective || "";
  el("resume-button").hidden = !detail.actions?.resume;
  el("retry-button").hidden = !detail.actions?.retry;
  el("review-button").hidden = !detail.actions?.review;
  el("cleanup-workspace-button").hidden = !detail.actions?.cleanupWorkspace;
  el("progress-label").textContent = `${run.completed || 0} / ${run.total || 0} complete`;
  el("progress-fill").style.width = `${run.total ? Math.round((run.completed / run.total) * 100) : 0}%`;
  renderTopMetrics(detail);
  renderWorkspaceBoundary(detail);
  renderDag(detail);
  const frontier = el("frontier-surface");
  frontier.hidden = !detail.frontierResult;
  if (detail.frontierResult) {
    el("frontier-usage").textContent = frontierUsageCopy(detail.frontierUsage);
  }
  renderFrontierReview(detail);
}

function renderWorkspaceBoundary(detail) {
  const panel = el("workspace-boundary");
  const workspace = detail.workspace;
  panel.hidden = !workspace;
  if (!workspace) return;
  el("workspace-branch").textContent = workspace.branch || "Session branch";
  el("workspace-root").textContent = workspace.workspaceRoot || "";
  el("workspace-shas").textContent = `${shortSha(workspace.baseSha)} → ${shortSha(workspace.headSha)}`;
  const target = workspace.executionPolicy?.workspaceTarget
    || detail.run.workspaceTarget
    || "worktree";
  const yolo = (
    workspace.executionPolicy?.approvalMode
    || detail.run.approvalMode
  ) === "yolo";
  el("workspace-path-label").textContent = target === "checkout"
    ? "Main checkout"
    : "Isolated worktree";
  el("workspace-path").textContent = workspace.cleanedUp
    ? "Worktree removed; branch retained"
    : workspace.executionPath || workspace.worktreePath || "";
  const dirty = el("workspace-dirty");
  setStateChip(dirty, workspace.dirty ? "rejected" : "completed");
  dirty.textContent = workspace.dirty ? "dirty source excluded" : "clean source";
  const entries = workspace.dirtyEntries || [];
  el("workspace-warning").textContent = target === "checkout"
    ? `${yolo ? "YOLO" : "Supervised"} main-checkout run: successful artifacts commit directly to ${workspace.branch}. Verification failure pauses without hiding the commit.`
    : workspace.dirty
      ? `The session started from committed HEAD. Local changes were excluded${entries.length ? `: ${entries.join(", ")}` : "."}`
      : `${yolo ? "YOLO auto-apply is active in the isolated worktree." : "The original checkout is never modified by artifact actions."}`;
  const finalDiff = detail.frontierResult?.workspace?.finalDiff;
  el("workspace-final-diff-wrap").hidden = typeof finalDiff !== "string";
  el("workspace-final-diff").textContent = finalDiff || "";
}

function renderTopMetrics(detail) {
  const status = displayStatus(detail.run);
  const runStatus = el("run-status");
  runStatus.replaceChildren();
  const dot = document.createElement("span");
  dot.className = `status-dot ${status}`;
  runStatus.append(dot, document.createTextNode(capitalize(status)));
  const usage = detail.localUsage || {};
  el("metric-elapsed").textContent = formatDuration(detail.run.elapsedSeconds);
  el("metric-tokens").textContent = formatNumber(localTokens(usage));
  const reviewPending = (
    !detail.frontierUsage?.review?.attemptedResponses
    && !["approved", "changes_requested", "rejected"].includes(
      detail.reviewStatus,
    )
  );
  el("metric-frontier-label").textContent = reviewPending
    ? "Frontier tokens so far"
    : "Frontier tokens";
  el("metric-frontier").textContent = frontierTokenMetric(detail.frontierUsage);
  el("metric-calls").textContent = formatNumber(usage.generationCalls || 0);
  el("metric-loads").textContent = formatNumber(usage.modelLoads || 0);
}

function renderFrontierReview(detail) {
  const panel = el("frontier-review-panel");
  const review = detail.frontierReview;
  const reviewStatus = detail.reviewStatus || "not_eligible";
  panel.hidden = !detail.frontierResult;
  if (panel.hidden) return;
  setStateChip(el("frontier-review-status"), reviewStatus);
  el("frontier-review-label").textContent = review
    ? `Frontier verdict: ${review.verdict.replaceAll("_", " ")}`
    : reviewStatus === "awaiting_review"
      ? "Frontier packet ready"
      : reviewStatus.replaceAll("_", " ");
  el("frontier-verdict").textContent = review
    ? capitalize(review.verdict.replaceAll("_", " "))
    : capitalize(reviewStatus.replaceAll("_", " "));
  el("frontier-summary").textContent = review?.summary
    || (reviewStatus === "awaiting_review"
      ? "The local DAG completed. One final frontier review is available."
      : detail.reviewError?.error || "No final frontier verdict is available.");
  const findings = el("frontier-findings");
  findings.replaceChildren();
  (review?.findings || []).forEach((finding) => {
    const block = document.createElement("div");
    block.className = `evidence-block ${finding.severity === "critical" || finding.severity === "high" ? "error" : ""}`;
    const label = document.createElement("div");
    label.className = "evidence-label";
    label.textContent = `${finding.severity}${finding.taskId ? ` · ${finding.taskId}` : ""}`;
    const title = document.createElement("strong");
    title.textContent = finding.title;
    const body = document.createElement("p");
    body.textContent = `${finding.evidence}\nRecommendation: ${finding.recommendation}`;
    block.append(label, title, body);
    findings.append(block);
  });
  const command = detail.commander?.reviewCommand || "";
  el("review-command").textContent = command;
  el("review-command").hidden = !detail.actions?.review;
  el("copy-review-command").hidden = !detail.actions?.review;
}

function frontierTokenMetric(frontierUsage) {
  const total = frontierUsage?.total;
  if (!total || (total.attemptedResponses || 0) === 0) return "—";
  if (total.usageStatus !== "reported") return "Unavailable";
  return formatNumber(total.totalTokens);
}

function frontierUsageCopy(frontierUsage) {
  const planningPhase = frontierUsage?.planning;
  const reviewPhase = frontierUsage?.review;
  const planning = planningPhase?.attemptedResponses || 0;
  const review = reviewPhase?.attemptedResponses || 0;
  const total = frontierUsage?.total;
  const calls = planning + review;
  if (!calls) return "No frontier response recorded";
  const phaseMetric = (label, phase) => {
    if (!phase?.attemptedResponses) return `${label} pending`;
    const outcome = phase.outcome === "invalid" ? " invalid" : "";
    if (phase.usageStatus !== "reported") return `${label}${outcome} unavailable`;
    return `${label}${outcome} ${formatNumber(phase.totalTokens)}`;
  };
  const combined = total?.usageStatus === "reported"
    ? `total ${formatNumber(total.totalTokens)}`
    : "combined unavailable";
  return `${phaseMetric("plan", planningPhase)} · ${phaseMetric("review", reviewPhase)} · ${combined}`;
}

function renderDag(detail) {
  const dag = el("dag");
  dag.replaceChildren();
  const definitions = new Map((detail.plan?.tasks || []).map((task) => [task.id, task]));
  let nodeIndex = 0;
  (detail.levels || []).forEach((level, levelIndex) => {
    const column = document.createElement("section");
    column.className = "dag-level";
    column.setAttribute("aria-label", `Execution wave ${levelIndex + 1}`);
    const label = document.createElement("span");
    label.className = "level-label";
    label.textContent = `Wave ${String(levelIndex + 1).padStart(2, "0")}`;
    column.append(label);
    level.forEach((taskId) => {
      nodeIndex += 1;
      const taskState = detail.tasks[taskId] || {};
      const taskDef = definitions.get(taskId) || {};
      const status = taskState.status || "pending";
      const button = document.createElement("button");
      button.type = "button";
      button.className = `dag-node ${status}${appState.selectedTask === taskId ? " selected" : ""}`;
      button.setAttribute("aria-pressed", appState.selectedTask === taskId ? "true" : "false");
      button.setAttribute("aria-label", `${taskId}, ${taskDef.role || taskState.role || "general"}, ${status}`);
      button.addEventListener("click", () => {
        appState.selectedTask = taskId;
        renderDag(appState.detail);
        renderInspector();
      });
      const top = document.createElement("span");
      top.className = "dag-node-top";
      const index = document.createElement("span");
      index.className = "node-index";
      index.textContent = String(nodeIndex).padStart(2, "0");
      const state = document.createElement("span");
      state.className = `node-status ${status}`;
      state.textContent = status;
      top.append(index, state);
      const name = document.createElement("strong");
      name.className = "node-name";
      name.textContent = taskId;
      const role = document.createElement("span");
      role.className = "node-role";
      role.textContent = taskDef.role || taskState.role || "general";
      const footer = document.createElement("span");
      footer.className = "node-footer";
      const deps = document.createElement("span");
      const dependencyCount = (taskDef.dependsOn || taskState.dependsOn || []).length;
      deps.textContent = dependencyCount ? `${dependencyCount} dep${dependencyCount === 1 ? "" : "s"}` : "root";
      const repairs = document.createElement("span");
      repairs.textContent = `${taskState.repairAttempts || 0} repair${taskState.repairAttempts === 1 ? "" : "s"}`;
      footer.append(deps, repairs);
      button.append(top, name, role, footer);
      column.append(button);
    });
    dag.append(column);
  });
}

function renderInspector() {
  if (["commander", "catalog"].includes(appState.viewMode)) {
    const plan = appState.requestDetail?.plan;
    const taskId = appState.selectedTask;
    const taskDef = (plan?.tasks || []).find((task) => task.id === taskId);
    if (!taskDef) {
      el("inspector-empty").hidden = false;
      el("inspector-content").hidden = true;
      return;
    }
    const taskState = {
      id: taskDef.id,
      role: taskDef.role,
      status: "pending",
      dependsOn: taskDef.dependsOn || [],
      repairAttempts: 0,
      output: null,
      normalizedOutput: null,
      gateResult: null,
      batchIndex: null,
    };
    el("inspector-empty").hidden = true;
    el("inspector-content").hidden = false;
    el("task-role").textContent = `${taskDef.role || "general"} worker · approval preview`;
    el("task-title").textContent = taskId;
    setStateChip(el("task-state-chip"), "pending");
    renderTabs();
    const panel = el("tab-panel");
    panel.replaceChildren();
    if (appState.activeTab === "overview") renderOverview(panel, taskDef, taskState);
    else if (appState.activeTab === "output") panel.append(emptyCopy("No local output exists before approval."));
    else if (appState.activeTab === "gate") renderGate(panel, taskDef, taskState);
    else panel.append(emptyCopy("Runtime evidence begins only after operator approval."));
    return;
  }
  const detail = appState.detail;
  const taskId = appState.selectedTask;
  if (!detail || !taskId || !detail.tasks[taskId]) {
    el("inspector-empty").hidden = false;
    el("inspector-content").hidden = true;
    return;
  }
  const taskState = detail.tasks[taskId];
  const taskDef = (detail.plan?.tasks || []).find((task) => task.id === taskId) || {};
  el("inspector-empty").hidden = true;
  el("inspector-content").hidden = false;
  el("task-role").textContent = `${taskDef.role || taskState.role || "general"} worker`;
  el("task-title").textContent = taskId;
  setStateChip(el("task-state-chip"), taskState.status || "pending");
  renderTabs();
  const panel = el("tab-panel");
  panel.replaceChildren();
  if (appState.activeTab === "overview") renderOverview(panel, taskDef, taskState);
  else if (appState.activeTab === "output") renderOutput(panel, taskState, taskId);
  else if (appState.activeTab === "gate") renderGate(panel, taskDef, taskState);
  else renderRuntime(panel, taskId, taskState);
}

function renderOverview(panel, taskDef, taskState) {
  const grid = document.createElement("dl");
  grid.className = "detail-grid";
  [
    ["Status", taskState.status || "pending"],
    ["Wave", taskState.batchIndex == null ? "—" : String(taskState.batchIndex + 1)],
    ["Dependencies", (taskDef.dependsOn || taskState.dependsOn || []).join(", ") || "None"],
    ["Repair attempts", `${taskState.repairAttempts || 0} / ${taskDef.maxRepairAttempts ?? "—"}`],
    ["Artifact type", taskDef.artifactType || taskState.artifactType || "report"],
    ["Worker output", taskDef.workerOutputProtocol || taskState.workerOutputProtocol || "artifact"],
    ["Allowed paths", (taskDef.allowedPaths || taskState.allowedPaths || []).join(", ") || "None"],
    ["Verification", (taskDef.verification || taskState.verification || []).join(", ") || "None"],
  ].forEach(([label, value]) => grid.append(detailCell(label, value)));
  panel.append(grid);
  panel.append(detailSection("Worker instruction", taskDef.prompt || "Historical prompt unavailable."));
  panel.append(detailSection("Output protocol", taskDef.outputProtocol || "Role default."));
  if (taskState.error) {
    const section = detailSection("Runtime error", taskState.error);
    section.querySelector("p").classList.add("failed");
    panel.append(section);
  }
}

function renderOutput(panel, taskState, taskId) {
  const artifact = appState.detail?.artifacts?.[taskId];
  if (artifact) {
    panel.append(codeSection(
      `${artifact.manifest.artifactType} · sha256:${artifact.manifest.sha256}`,
      artifact.payload || "",
    ));
    renderArtifactActions(panel, taskId, artifact);
    if ((artifact.verification || []).length) {
      panel.append(codeSection(
        "Verification evidence",
        JSON.stringify(artifact.verification, null, 2),
      ));
    }
    return;
  }
  const normalized = taskState.normalizedOutput;
  const raw = taskState.output;
  if (!normalized && !raw) {
    panel.append(emptyCopy("No task output has been written yet."));
    return;
  }
  if (normalized) panel.append(codeSection("Normalized output", normalized));
  if (raw != null) panel.append(codeSection(normalized === raw ? "Raw output" : "Raw output available", raw));
}

function renderArtifactActions(panel, taskId, artifact) {
  const actions = document.createElement("div");
  actions.className = "artifact-actions";
  if (artifact.actions?.apply) {
    actions.append(actionButton("Apply to session branch", "primary", () =>
      decideArtifact(taskId, "apply", artifact.manifest.sha256)));
  }
  if (artifact.actions?.verify) {
    actions.append(actionButton("Rerun approved checks", "secondary", () =>
      decideArtifact(taskId, "verify", artifact.manifest.sha256)));
  }
  if (artifact.actions?.reject) {
    actions.append(actionButton(
      artifact.status === "verification_failed" ? "Reject and revert" : "Reject artifact",
      "danger",
      () => decideArtifact(taskId, "reject", artifact.manifest.sha256),
    ));
  }
  if (actions.childElementCount) panel.append(actions);
}

function actionButton(label, style, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${style}`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function renderGate(panel, taskDef, taskState) {
  const result = taskState.gateResult;
  if (!result) {
    panel.append(emptyCopy("This task has not produced gate evidence yet."));
  } else {
    const summary = document.createElement("div");
    summary.className = `evidence-block ${result.passed ? "success" : "error"}`;
    const label = document.createElement("div");
    label.className = "evidence-label";
    label.textContent = "Gate decision";
    summary.append(label, document.createTextNode(result.passed ? "Passed" : "Rejected"));
    panel.append(summary);
    const violations = result.violations || [];
    panel.append(evidenceList(
      "Violations",
      violations.map((v) => `${v.id}: ${v.message || v.detail || "Gate condition failed"}`),
      "No violations recorded.", true,
    ));
    panel.append(evidenceList(
      "Normalizations", result.normalizations || [], "No output normalizations applied.", true,
    ));
  }
  if (taskDef.gate) panel.append(codeSection("Configured gate", JSON.stringify(taskDef.gate, null, 2)));
  else panel.append(detailSection("Configured gate", "No explicit gate configured."));
}

function renderRuntime(panel, taskId, taskState) {
  const batches = (appState.detail?.batches || []).filter(
    (batch) => (batch.taskIds || []).includes(taskId));
  const grid = document.createElement("dl");
  grid.className = "detail-grid";
  grid.append(
    detailCell("Batch index", taskState.batchIndex == null ? "—" : String(taskState.batchIndex)),
    detailCell("Repair attempts", String(taskState.repairAttempts || 0)),
    detailCell("Raw output", taskState.output == null ? "Unavailable" : "Available"),
    detailCell("Runner log", appState.detail?.runnerLogAvailable ? "Available" : "Unavailable"),
    detailCell("Artifact", taskState.artifact?.sha256 || "Unavailable"),
  );
  panel.append(grid);
  if (appState.detail?.localExecutionProfile) {
    panel.append(codeSection(
      "Local execution profile",
      JSON.stringify(appState.detail.localExecutionProfile, null, 2),
    ));
  }
  const artifact = appState.detail?.artifacts?.[taskId];
  if (artifact?.verification?.length) {
    panel.append(codeSection(
      "Allowlisted verification",
      artifact.verification.map((result) => [
        `${result.profileId}: ${result.passed ? "passed" : "failed"}`,
        `$ ${result.argv.join(" ")}`,
        result.log || "(no output)",
      ].join("\n")).join("\n\n"),
    ));
  }
  if (!batches.length) {
    panel.append(emptyCopy("No batch statistics are associated with this task yet."));
    return;
  }
  const rows = [];
  batches.forEach((batch) => {
    rows.push({label: `Wave ${batch.levelIndex + 1}`, stats: batch.statistics || {}});
    (batch.repairs || []).forEach((repair) => {
      if ((repair.taskIds || []).includes(taskId)) {
        rows.push({label: `Repair ${repair.round}`, stats: repair.statistics || {}});
      }
    });
  });
  panel.append(metricTable(rows));
  panel.append(codeSection("Batch evidence", JSON.stringify(batches, null, 2)));
}

function detailCell(label, value) {
  const wrapper = document.createElement("div");
  wrapper.className = "detail-cell";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  wrapper.append(term, description);
  return wrapper;
}

function detailSection(title, text) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const body = document.createElement("p");
  body.textContent = text;
  section.append(heading, body);
  return section;
}

function codeSection(title, text) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const pre = document.createElement("pre");
  pre.className = "code-block";
  const code = document.createElement("code");
  code.textContent = text;
  pre.append(code);
  section.append(heading, pre);
  return section;
}

function evidenceList(title, items, emptyMessage, successWhenEmpty) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("div");
  list.className = "evidence-list";
  if (!items.length) {
    const block = document.createElement("div");
    block.className = `evidence-block${successWhenEmpty ? " success" : ""}`;
    block.textContent = emptyMessage;
    list.append(block);
  } else {
    items.forEach((item) => {
      const block = document.createElement("div");
      block.className = "evidence-block error";
      block.textContent = String(item);
      list.append(block);
    });
  }
  section.append(heading, list);
  return section;
}

function metricTable(rows) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = "Generation statistics";
  const table = document.createElement("table");
  table.className = "metric-table";
  const header = table.createTHead().insertRow();
  ["Pass", "Prompt", "Generated", "Seconds"].forEach((name) => {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = name;
    header.append(cell);
  });
  const body = table.createTBody();
  rows.forEach(({label, stats}) => {
    const row = body.insertRow();
    [
      label, formatNumber(stats.promptTokens || 0), formatNumber(stats.generationTokens || 0),
      Number(stats.generationSeconds || stats.elapsedSeconds || 0).toFixed(2),
    ].forEach((value) => {
      const cell = row.insertCell();
      cell.textContent = value;
    });
  });
  section.append(heading, table);
  return section;
}

function emptyCopy(message) {
  const empty = document.createElement("div");
  empty.className = "empty-copy";
  empty.textContent = message;
  return empty;
}

function selectTab(name) {
  appState.activeTab = name;
  renderInspector();
  el(`tab-${name}`).focus();
}
function renderTabs() {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const selected = button.dataset.tab === appState.activeTab;
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.tabIndex = selected ? 0 : -1;
  });
}
function tabKeydown(event) {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const tabs = Array.from(document.querySelectorAll("[data-tab]"));
  const current = tabs.indexOf(event.currentTarget);
  const next = (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  selectTab(tabs[next].dataset.tab);
}

async function createCommanderRequest(event) {
  event.preventDefault();
  const objective = el("commander-objective").value.trim();
  if (!objective) return;
  const constraints = el("commander-constraints").value
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean);
  const revisionOf = el("commander-revision").value.trim();
  setActionBusy(el("commander-create"), true, "Creating…");
  try {
    const detail = await api("/api/commander/requests", {
      method: "POST",
      body: JSON.stringify({
        objective,
        constraints,
        ...(revisionOf ? {revisionOf} : {}),
      }),
    });
    appState.commanderRequests.unshift({
      requestId: detail.request.requestId,
      objective: detail.request.objective,
      status: detail.request.status,
      createdAt: detail.request.createdAt,
      updatedAt: detail.request.updatedAt,
    });
    appState.requestDetail = detail;
    appState.selectedRequest = detail.request.requestId;
    appState.selectedRun = null;
    appState.selectedTask = null;
    appState.viewMode = "commander";
    renderCommanderRequests();
    el("commander-select").value = detail.request.requestId;
    renderCommanderPreview();
    renderInspector();
    showToast("Planning request created. Copy the Codex handoff.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setActionBusy(el("commander-create"), false, "Create planning request");
    schedulePoll();
  }
}

async function copyPlanCommand() {
  const command = appState.requestDetail?.handoff?.planCommand;
  if (command) await copyText(command, "Plan handoff copied.");
}

async function approveCommanderRun() {
  const detail = appState.requestDetail;
  const requestId = detail?.request?.requestId;
  const planDigest = detail?.request?.planDigest;
  if (!requestId || !planDigest) return;
  const execution = selectedExecutionPreview(detail);
  if (
    el("workspace-target").value === "checkout"
    && !window.confirm(
      "Main-checkout YOLO commits successful local artifacts directly to the current branch. Continue?",
    )
  ) return;
  setActionBusy(el("approve-run-button"), true, "Approving…");
  try {
    const runDetail = await api(
      `/api/commander/requests/${encodeURIComponent(requestId)}/approve-run`,
      {
        method: "POST",
        body: JSON.stringify({
          planDigest,
          executionDigest: execution?.executionDigest,
          maxRepair: Number(el("max-repair").value),
          approvalMode: el("approval-mode").value,
          workspaceTarget: el("workspace-target").value,
        }),
      },
    );
    appState.selectedRequest = null;
    adoptNewRun(runDetail);
    showToast(`Approved digest ${planDigest.slice(0, 12)}… and launched local work.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setActionBusy(el("approve-run-button"), false, "Approve and run");
    schedulePoll();
  }
}

async function copyReviewCommand() {
  const command = appState.detail?.commander?.reviewCommand;
  if (command) await copyText(command, "Final-review handoff copied.");
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(value);
    showToast(successMessage);
  } catch (_error) {
    showToast("Clipboard access failed. Select and copy the displayed command.", true);
  }
}

async function launchRun(event) {
  event.preventDefault();
  const planId = el("plan-select").value;
  if (!planId) return;
  if (
    el("workspace-target").value === "checkout"
    && !window.confirm(
      "Main-checkout YOLO commits successful local artifacts directly to the current branch. Continue?",
    )
  ) return;
  setActionBusy(el("launch-button"), true, "Launching…");
  try {
    const plan = appState.plans.find((item) => item.planId === planId);
    const execution = selectedExecutionPreview(plan);
    const detail = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        planId,
        maxRepair: Number(el("max-repair").value),
        ...(plan?.schemaVersion >= 2 ? {
          planDigest: plan.digest,
          executionDigest: execution?.executionDigest,
          approvalMode: el("approval-mode").value,
          workspaceTarget: el("workspace-target").value,
        } : {}),
      }),
    });
    adoptNewRun(detail);
    showToast(`Launched ${planId}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    const selectedPlan = appState.plans.find(
      (item) => item.planId === el("plan-select").value);
    setActionBusy(
      el("launch-button"),
      false,
      selectedPlan?.schemaVersion >= 2
        ? el("approval-mode").value === "yolo"
          ? "Approve YOLO run"
          : "Approve and launch"
        : "Launch run",
    );
    schedulePoll();
  }
}

async function resumeRun() {
  if (!appState.detail) return;
  const {planId, sessionId} = appState.detail.run;
  setActionBusy(el("resume-button"), true, "Resuming…");
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(planId)}/${encodeURIComponent(sessionId)}/resume`, {
      method: "POST", body: "{}",
    });
    appState.detail = detail;
    updateRunInList(detail.run);
    renderRun();
    renderInspector();
    showToast("Run resumed.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setActionBusy(el("resume-button"), false, "Resume");
    schedulePoll();
  }
}

async function retryRun() {
  if (!appState.detail) return;
  const {planId, sessionId} = appState.detail.run;
  setActionBusy(el("retry-button"), true, "Starting retry…");
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(planId)}/${encodeURIComponent(sessionId)}/retry`, {
      method: "POST", body: JSON.stringify({
        maxRepair: Number(el("max-repair").value),
        approvalMode: appState.detail.run.approvalMode || "supervised",
        workspaceTarget: appState.detail.run.workspaceTarget || "worktree",
        ...(appState.detail.retryExecutionPreview?.executionDigest
          ? {executionDigest: appState.detail.retryExecutionPreview.executionDigest}
          : {}),
      }),
    });
    adoptNewRun(detail);
    showToast(`New linked run ${shortSession(detail.run.sessionId)} started.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setActionBusy(el("retry-button"), false, "Retry as new run");
    schedulePoll();
  }
}

async function decideArtifact(taskId, action, digest) {
  if (!appState.detail) return;
  const verb = action === "apply"
    ? "apply this exact diff to the isolated session branch"
    : action === "reject"
      ? "reject this artifact"
      : "rerun the configured verification profiles";
  if (!window.confirm(`Confirm: ${verb}?`)) return;
  const {planId, sessionId} = appState.detail.run;
  try {
    const detail = await api(
      `/api/runs/${encodeURIComponent(planId)}/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(taskId)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify({artifactDigest: digest}),
      },
    );
    appState.detail = detail;
    updateRunInList(detail.run);
    renderRun();
    renderInspector();
    showToast(`${capitalize(action)} decision recorded for ${taskId}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    schedulePoll();
  }
}

async function cleanupWorkspace() {
  if (!appState.detail) return;
  if (!window.confirm("Remove this isolated worktree? The session branch and audit artifacts will remain.")) return;
  const {planId, sessionId} = appState.detail.run;
  setActionBusy(el("cleanup-workspace-button"), true, "Removing…");
  try {
    appState.detail = await api(
      `/api/runs/${encodeURIComponent(planId)}/${encodeURIComponent(sessionId)}/workspace/cleanup`,
      {method: "POST", body: "{}"},
    );
    renderRun();
    renderInspector();
    showToast("Worktree removed; session branch retained.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setActionBusy(el("cleanup-workspace-button"), false, "Remove worktree");
  }
}

function adoptNewRun(detail) {
  appState.detail = detail;
  appState.requestDetail = null;
  appState.selectedRequest = null;
  appState.selectedRun = runKey(detail.run);
  appState.selectedTask = firstInterestingTask(detail);
  appState.viewMode = "run";
  el("commander-select").value = "";
  updateRunInList(detail.run);
  renderRuns();
  renderRun();
  renderInspector();
}
function setActionBusy(button, busy, label) {
  button.disabled = busy;
  const labelNode = button.querySelector("span") || button;
  labelNode.textContent = label;
}
function clearSelection() {
  appState.detail = null;
  appState.requestDetail = null;
  appState.selectedRun = null;
  appState.selectedRequest = null;
  appState.selectedTask = null;
  appState.viewMode = null;
  el("empty-state").hidden = false;
  el("run-workspace").hidden = true;
  el("commander-workspace").hidden = true;
  el("inspector-empty").hidden = false;
  el("inspector-content").hidden = true;
  const node = el("run-status");
  node.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "status-dot pending";
  node.append(dot, document.createTextNode("No run"));
  ["metric-elapsed", "metric-tokens", "metric-frontier", "metric-calls", "metric-loads"].forEach((id) => {
    el(id).textContent = "—";
  });
}
function schedulePoll() {
  clearPoll();
  if (document.hidden) return;
  const run = appState.detail?.run;
  const live = run && (run.active || ["pending", "running", "awaiting_approval"].includes(run.status));
  appState.timer = window.setTimeout(() => refreshAll(false), live ? 1000 : 5000);
}
function clearPoll() {
  if (appState.timer) {
    window.clearTimeout(appState.timer);
    appState.timer = null;
  }
}
function showToast(message, isError = false) {
  const toast = el("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  toast.hidden = false;
  if (appState.toastTimer) window.clearTimeout(appState.toastTimer);
  appState.toastTimer = window.setTimeout(() => { toast.hidden = true; }, isError ? 6500 : 3200);
}
function firstInterestingTask(detail) {
  const entries = Object.entries(detail.tasks || {});
  const interesting = entries.find(([, task]) => [
    "awaiting_approval", "verification_failed", "running", "rejected", "failed",
  ].includes(task.status));
  return (interesting || entries[0] || [null])[0];
}
function displayStatus(run) {
  return run?.active && run.status === "pending" ? "running" : (run?.status || "pending");
}
function setStateChip(node, status) {
  node.className = `state-chip ${status}`;
  node.textContent = status.replaceAll("_", " ");
}
function runKey(run) { return `${run.planId}/${run.sessionId}`; }
function splitRunKey(key) {
  const index = key.indexOf("/");
  return [key.slice(0, index), key.slice(index + 1)];
}
function shortSession(sessionId) {
  if (!sessionId) return "—";
  const parts = sessionId.split("-");
  return parts[parts.length - 1] || sessionId;
}
function shortSha(value) { return value ? String(value).slice(0, 12) : "—"; }
function localTokens(usage) {
  return Number(usage.promptTokens || 0) + Number(usage.generationTokens || 0);
}
function formatNumber(value) { return new Intl.NumberFormat().format(Number(value || 0)); }
function formatDuration(seconds) {
  if (seconds == null) return "—";
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
function capitalize(value) { return value ? value.charAt(0).toUpperCase() + value.slice(1) : ""; }

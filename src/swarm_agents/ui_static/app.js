// @lat: [[UI]]
"use strict";

const appState = {
  system: null,
  plans: [],
  invalidPlans: [],
  runs: [],
  detail: null,
  selectedRun: null,
  selectedTask: null,
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
  el("launch-form").addEventListener("submit", launchRun);
  el("plan-select").addEventListener("change", renderPlanDescription);
  el("run-search").addEventListener("input", renderRuns);
  el("refresh-button").addEventListener("click", () => refreshAll(true));
  el("resume-button").addEventListener("click", resumeRun);
  el("retry-button").addEventListener("click", retryRun);
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
    const [system, plansPayload, runsPayload] = await Promise.all([
      api("/api/status"), api("/api/plans"), api("/api/runs"),
    ]);
    appState.system = system;
    appState.plans = plansPayload.plans || [];
    appState.invalidPlans = plansPayload.invalid || [];
    appState.runs = runsPayload.runs || [];
    renderSystem();
    renderPlans();
    renderRuns();

    if (appState.selectedRun) {
      const exists = appState.runs.some((run) => runKey(run) === appState.selectedRun);
      if (exists) await loadRunByKey(appState.selectedRun, false);
      else clearSelection();
    } else if (appState.runs.length) {
      await selectRun(appState.runs[0]);
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
  renderPlanDescription();
}

function renderPlanDescription() {
  const plan = appState.plans.find((item) => item.planId === el("plan-select").value);
  el("plan-description").textContent = plan
    ? plan.objective
    : "Select a validated plan from the configured directory.";
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
    meta.append(session, progress);
    main.append(top, meta);
    button.append(accent, main);
    list.append(button);
  });
}

async function selectRun(run) {
  appState.selectedRun = runKey(run);
  appState.selectedTask = null;
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
  el("run-workspace").hidden = false;
  const run = detail.run;
  const status = displayStatus(run);
  setStateChip(el("run-state-chip"), status);
  el("run-session-id").textContent = run.sessionId || "";
  el("dag-title").textContent = run.planId || "Run";
  el("run-objective").textContent = detail.plan?.objective || run.objective || "";
  el("resume-button").hidden = !detail.actions?.resume;
  el("retry-button").hidden = !detail.actions?.retry;
  el("progress-label").textContent = `${run.completed || 0} / ${run.total || 0} complete`;
  el("progress-fill").style.width = `${run.total ? Math.round((run.completed / run.total) * 100) : 0}%`;
  renderTopMetrics(detail);
  renderDag(detail);
  const frontier = el("frontier-surface");
  frontier.hidden = !detail.frontierResult;
  if (detail.frontierResult) {
    const usage = detail.localUsage || {};
    el("frontier-usage").textContent =
      `${formatNumber(localTokens(usage))} local tokens · ${formatNumber(usage.generationCalls || 0)} calls`;
  }
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
  el("metric-calls").textContent = formatNumber(usage.generationCalls || 0);
  el("metric-loads").textContent = formatNumber(usage.modelLoads || 0);
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
  else if (appState.activeTab === "output") renderOutput(panel, taskState);
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

function renderOutput(panel, taskState) {
  const normalized = taskState.normalizedOutput;
  const raw = taskState.output;
  if (!normalized && !raw) {
    panel.append(emptyCopy("No task output has been written yet."));
    return;
  }
  if (normalized) panel.append(codeSection("Normalized output", normalized));
  if (raw != null) panel.append(codeSection(normalized === raw ? "Raw output" : "Raw output available", raw));
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
  );
  panel.append(grid);
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

async function launchRun(event) {
  event.preventDefault();
  const planId = el("plan-select").value;
  if (!planId) return;
  setActionBusy(el("launch-button"), true, "Launching…");
  try {
    const detail = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({planId, maxRepair: Number(el("max-repair").value)}),
    });
    adoptNewRun(detail);
    showToast(`Launched ${planId}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setActionBusy(el("launch-button"), false, "Launch run");
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
      method: "POST", body: JSON.stringify({maxRepair: Number(el("max-repair").value)}),
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

function adoptNewRun(detail) {
  appState.detail = detail;
  appState.selectedRun = runKey(detail.run);
  appState.selectedTask = firstInterestingTask(detail);
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
  appState.selectedRun = null;
  appState.selectedTask = null;
  el("empty-state").hidden = false;
  el("run-workspace").hidden = true;
  el("inspector-empty").hidden = false;
  el("inspector-content").hidden = true;
  const node = el("run-status");
  node.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "status-dot pending";
  node.append(dot, document.createTextNode("No run"));
  ["metric-elapsed", "metric-tokens", "metric-calls", "metric-loads"].forEach((id) => {
    el(id).textContent = "—";
  });
}
function schedulePoll() {
  clearPoll();
  if (document.hidden) return;
  const run = appState.detail?.run;
  const live = run && (run.active || ["pending", "running"].includes(run.status));
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
  const interesting = entries.find(([, task]) => ["running", "rejected", "failed"].includes(task.status));
  return (interesting || entries[0] || [null])[0];
}
function displayStatus(run) {
  return run?.active && run.status === "pending" ? "running" : (run?.status || "pending");
}
function setStateChip(node, status) {
  node.className = `state-chip ${status}`;
  node.textContent = status;
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

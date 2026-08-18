import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import {
  ApprovalCard,
  ContextCards,
  DiffReview,
  explainViolation,
  matchesRequestFilter,
  matchesRunFilter,
  PlanReview,
  ProjectSidebar,
  RunActions,
  SkillGuide,
  NewTask,
  TaskInspector,
  clampTerminalSplit,
  newlyReadyPlan,
  TaskRows,
  unresolvedRunCounts,
} from "./components";
import { ANSI_THEME_KEYS, TERMINAL_THEME, TerminalPanel } from "./TerminalPanel";

test("approval card keeps human action explicit", () => {
  render(
    <ApprovalCard
      title="Approve this plan?"
      detail="Digest-bound execution"
      primaryLabel="Approve and run"
      onPrimary={() => undefined}
    />,
  );
  expect(screen.getByRole("button", { name: "Approve and run" })).toBeVisible();
  expect(screen.getByText("Digest-bound execution")).toBeVisible();
});

test("task detail is a complementary inspector panel", () => {
  render(
    <TaskInspector
      taskId="add-styles"
      task={{ status: "awaiting_approval", output: "patch ready" }}
      artifact={{
        manifest: { sha256: "a".repeat(64), artifactType: "unified-diff" },
        payload: "--- a/website/styles.css\n+++ b/website/styles.css\n",
        status: "awaiting_approval",
        actions: { apply: true, reject: true, verify: false },
      }}
      onRefresh={() => undefined}
      onDecide={() => undefined}
    />,
  );
  expect(screen.getByRole("complementary", { name: "Task detail" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "add-styles" })).toBeVisible();
  expect(screen.getByText(/--- a\/website\/styles.css/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Apply" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Reject" })).toBeVisible();
});

test("a live plan shows which task is executing and hides prompt dumps", () => {
  render(
    <PlanReview
      plan={{
        objective: "Add retry logic",
        digest: "c".repeat(64),
        levels: [["edit"], ["test"]],
        tasks: [
          { id: "edit", role: "implementation", prompt: "Rewrite the retry loop" },
          { id: "test", role: "test", prompt: "Cover the retry", dependsOn: ["edit"] },
        ],
      }}
      planPrompt={{ requestId: "req", prompt: "Plan this objective", sha256: "d".repeat(64) }}
      taskStates={{
        edit: { status: "awaiting_approval" },
        test: { status: "pending" },
      }}
      artifacts={{
        edit: {
          manifest: { sha256: "a".repeat(64), artifactType: "unified-diff" },
          payload: "--- a\n+++ b\n",
          status: "awaiting_approval",
          actions: { apply: true, reject: true, verify: false },
        },
      }}
    />,
  );
  expect(screen.getByText("Approval")).toBeVisible();
  expect(screen.getByText("1 patch is waiting. Apply or reject on the highlighted tasks.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Apply" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Reject" })).toBeVisible();
  expect(screen.getByText("pending")).toBeVisible();
  expect(screen.queryByText("Planning prompt")).not.toBeInTheDocument();
  expect(screen.queryByText("Rewrite the retry loop")).not.toBeInTheDocument();
});

test("plan review exposes waves, task prompts, and bound digests", () => {
  render(
    <PlanReview
      plan={{
        objective: "Add retry logic",
        digest: "c".repeat(64),
        levels: [["edit"], ["test"]],
        tasks: [
          {
            id: "edit",
            role: "implementation",
            prompt: "Rewrite the retry loop",
            artifactType: "unified-diff",
            allowedPaths: ["src/retry.py"],
          },
          { id: "test", role: "test", prompt: "Cover the retry", dependsOn: ["edit"] },
        ],
      }}
      planPrompt={{ requestId: "req", prompt: "Plan this objective", sha256: "d".repeat(64) }}
      executionDigest={"e".repeat(64)}
    />,
  );
  expect(screen.getByText("Add retry logic")).toBeVisible();
  expect(screen.getByText("2 tasks")).toBeVisible();
  expect(screen.getByText("Wave 2")).toBeVisible();
  expect(screen.getByText("Rewrite the retry loop")).toBeInTheDocument();
  expect(screen.getByText("src/retry.py")).toBeInTheDocument();
  expect(screen.getByText("test · after edit")).toBeVisible();
  expect(screen.getByText(`plan ${"c".repeat(12)}`)).toBeVisible();
  expect(screen.getByText(`exec ${"e".repeat(12)}`)).toBeVisible();
  expect(screen.getByText("Planning prompt")).toBeVisible();
});

test("task rows render live state and gate chips", () => {
  render(
    <TaskRows
      detail={{
        run: { planId: "p", sessionId: "s", status: "running" },
        levels: [["edit"]],
        tasks: {
          edit: { status: "completed", gateResult: { passed: true } },
        },
      }}
      onSelect={() => undefined}
    />,
  );
  expect(screen.getByText("edit")).toBeVisible();
  expect(screen.getByText("Completed")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /edit/ }));
  expect(screen.getByText("Gate passed")).toBeVisible();
});

test("task rows surface the agent outcome without opening the inspector", () => {
  render(
    <TaskRows
      detail={{
        run: { planId: "p", sessionId: "s", status: "partial" },
        levels: [["report"]],
        tasks: {
          report: {
            status: "completed",
            gateResult: { passed: true },
            normalizedOutput: "## CEILING\nThe reasoning ceiling is 1536 tokens.",
            artifact: { artifactType: "report", sha256: "a".repeat(64) },
          },
        },
      }}
      onSelect={() => undefined}
    />,
  );
  expect(screen.getByText("report · 48 chars")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /report/ }));
  expect(
    screen.getByText("## CEILING · The reasoning ceiling is 1536 tokens."),
  ).toBeVisible();
});

test("context cards render prompts as text", () => {
  render(
    <ContextCards
      payload={{
        taskId: "edit",
        attempts: [{
          kind: "generation",
          attempt: 1,
          phase: "generation",
          promptSha256: "a".repeat(64),
          outputSha256: "b".repeat(64),
          prompt: "<script>unsafe()</script>",
          output: "done",
          authoritative: true,
        }],
      }}
    />,
  );
  expect(screen.getByText("<script>unsafe()</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
});

test("diff review renders line-oriented changes", () => {
  render(
    <DiffReview
      split={false}
      diff={{
        raw: "",
        summary: { files: 1, additions: 1, deletions: 0 },
        files: [{
          oldPath: "src/a.py",
          newPath: "src/a.py",
          status: "modified",
          additions: 1,
          deletions: 0,
          hunks: [{
            header: "@@ -1 +1,2 @@",
            label: "",
            lines: [
              { type: "context", oldLine: 1, newLine: 1, text: "before" },
              { type: "add", oldLine: null, newLine: 2, text: "after" },
            ],
          }],
        }],
      }}
    />,
  );
  expect(screen.getByText("src/a.py")).toBeVisible();
  expect(screen.getByText("after")).toBeVisible();
});

const partialRun = {
  planId: "reasoning-truncation-exposure",
  sessionId: "20260818T110338Z-5784cc8c",
  objective: "Analyze the local reasoning stage",
  status: "partial",
};

test("a partial run with every task done counts as finished, not running", () => {
  expect(matchesRunFilter(partialRun, "finished", "")).toBe(true);
  expect(matchesRunFilter(partialRun, "running", "")).toBe(false);
  expect(matchesRunFilter(partialRun, "all", "")).toBe(true);
});

test("run search matches plan id, session id, and objective", () => {
  expect(matchesRunFilter(partialRun, "all", "reasoning")).toBe(true);
  expect(matchesRunFilter(partialRun, "all", "5784cc8c")).toBe(true);
  expect(matchesRunFilter(partialRun, "all", "  Analyze  ")).toBe(true);
  expect(matchesRunFilter(partialRun, "all", "landfall")).toBe(false);
});

test("a launched request leaves the planning inbox", () => {
  const launched = {
    requestId: "request-20260818t105142z-cc809cd0",
    objective: "Analyze the local reasoning stage",
    status: "launched",
    sessionRef: "reasoning-truncation-exposure/20260818T110338Z-5784cc8c",
  };
  expect(matchesRequestFilter(launched, "all", "")).toBe(false);
  expect(matchesRequestFilter(launched, "approval", "")).toBe(false);
});

test("a request still awaiting a plan stays in the inbox and is searchable", () => {
  const pending = {
    requestId: "request-20260818t112848z-f8197c17",
    objective: "Draft the onboarding checklist",
    status: "awaiting_plan",
  };
  expect(matchesRequestFilter(pending, "all", "")).toBe(true);
  expect(matchesRequestFilter(pending, "all", "onboarding")).toBe(true);
  expect(matchesRequestFilter(pending, "all", "landfall")).toBe(false);
  // Run only filters never surface the inbox.
  expect(matchesRequestFilter(pending, "running", "")).toBe(false);
  expect(matchesRequestFilter(pending, "finished", "")).toBe(false);
});

test("a rejected task shows why it was rejected, not just a red chip", () => {
  render(
    <TaskRows
      detail={{
        run: { planId: "p", sessionId: "s", status: "partial" },
        levels: [["markup-review"]],
        tasks: {
          "markup-review": {
            status: "rejected",
            gateResult: {
              passed: false,
              violations: [
                { id: "workspace-artifact", kind: "workspace", message: "Review artifacts must be exact JSON objects." },
              ],
            },
          },
        },
      }}
      onSelect={() => undefined}
    />,
  );
  expect(screen.getByText(/Review artifacts must be exact JSON objects\./)).toBeVisible();
});

test("completed task rows stay collapsed until clicked", () => {
  const selected: string[] = [];
  render(
    <TaskRows
      detail={{
        run: { planId: "p", sessionId: "s", status: "completed" },
        levels: [["edit"]],
        tasks: { edit: { status: "completed", gateResult: { passed: true } } },
      }}
      onSelect={(taskId) => selected.push(taskId)}
    />,
  );
  const row = screen.getByRole("button", { name: /edit/ });
  expect(row).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(row);
  expect(row).toHaveAttribute("aria-expanded", "true");
  expect(selected).toEqual(["edit"]);
});

test("a completed task shows no failure reason", () => {
  render(
    <TaskRows
      detail={{
        run: { planId: "p", sessionId: "s", status: "partial" },
        levels: [["ok"]],
        tasks: { ok: { status: "completed", gateResult: { passed: true } } },
      }}
      onSelect={() => undefined}
    />,
  );
  expect(document.querySelector(".task-reason")).toBeNull();
});

test("run rows count every task status that is not completed", () => {
  expect(
    unresolvedRunCounts({
      planId: "landfall-landing-page",
      sessionId: "s",
      status: "partial",
      counts: { completed: 8, rejected: 1 },
    }),
  ).toEqual([["rejected", 1]]);
  expect(
    unresolvedRunCounts({
      planId: "p",
      sessionId: "s",
      status: "completed",
      counts: { completed: 3 },
    }),
  ).toEqual([]);
});

test("a rejected task explains whether re-running can clear it", () => {
  expect(explainViolation("workspace")).toContain("only after the plan is corrected");
  expect(explainViolation("size")).toContain("raises the bounded ceiling once");
  expect(explainViolation("required-pattern")).toContain("gate feedback");
  expect(explainViolation("nonsense")).toBe("");
});

test("the rejection reason on the row carries the explanation", () => {
  render(
    <TaskRows
      detail={{
        run: { planId: "p", sessionId: "s", status: "partial" },
        levels: [["markup-review"]],
        tasks: {
          "markup-review": {
            status: "rejected",
            gateResult: {
              passed: false,
              violations: [
                { id: "workspace-artifact", kind: "workspace", message: "Review artifacts must be exact JSON objects." },
              ],
            },
          },
        },
      }}
      onSelect={() => undefined}
    />,
  );
  expect(
    screen.getByText(/Review artifacts must be exact JSON objects\..*plan is corrected/),
  ).toBeVisible();
});

test("a stopped run offers a way forward and names what is stuck", async () => {
  const clicked: string[] = [];
  render(
    <RunActions
      detail={{
        run: { planId: "landfall-landing-page", sessionId: "s", status: "partial" },
        tasks: {
          "section-hero": { status: "completed" },
          "markup-review": { status: "rejected" },
        },
        actions: { resume: false, retry: true, cleanupWorkspace: true },
      }}
      onAction={(action) => clicked.push(action)}
    />,
  );
  expect(screen.getByText(/markup-review did not finish/)).toBeVisible();
  expect(screen.queryByRole("button", { name: /Resume/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Run the plan again" }));
  expect(clicked).toEqual(["retry"]);
});

test("a healthy run shows no action bar", () => {
  const { container } = render(
    <RunActions
      detail={{
        run: { planId: "p", sessionId: "s", status: "completed" },
        tasks: { a: { status: "completed" } },
        actions: { resume: false, retry: false, cleanupWorkspace: false },
      }}
      onAction={() => undefined}
    />,
  );
  expect(container.firstChild).toBeNull();
});

test("project sidebar lists plans and reviews for the opened folder", () => {
  const selected: string[] = [];
  render(
    <ProjectSidebar
      projectName="swarm-agents"
      requests={[{ requestId: "req-1", objective: "Add retry", status: "plan_ready", planDigest: "aa" }]}
      runs={[{ planId: "p", sessionId: "s", status: "completed", objective: "Add retry" }]}
      selectedRequestId="req-1"
      selectedRunKey={null}
      canOpenFolder
      onOpenFolder={() => selected.push("open")}
      onSelectRequest={(id) => selected.push(id)}
      onSelectReview={(run) => selected.push(`${run.planId}/${run.sessionId}`)}
      onNewTask={() => selected.push("task")}
    />,
  );
  expect(screen.getByText("swarm-agents")).toBeVisible();
  expect(screen.getByText("Plans")).toBeVisible();
  expect(screen.getByText("Reviews")).toBeVisible();
  expect(screen.getAllByText("Add retry")[0].className).toMatch(/truncate/);
  fireEvent.click(screen.getByRole("button", { name: /plan ready/i }));
  expect(selected).toEqual(["req-1"]);
  fireEvent.click(screen.getByRole("button", { name: /completed/i }));
  expect(selected).toEqual(["req-1", "p/s"]);
});

test("a launched request leaves plans and is only the live run", () => {
  render(
    <ProjectSidebar
      projectName="swarm-agents"
      requests={[{
        requestId: "req-1",
        objective: "Landing page",
        status: "launched",
        sessionRef: "p/s",
      }]}
      runs={[{ planId: "p", sessionId: "s", status: "awaiting_approval", objective: "Landing page" }]}
      selectedRequestId={null}
      selectedRunKey="p/s"
      canOpenFolder={false}
      onSelectRequest={() => undefined}
      onSelectReview={() => undefined}
      onNewTask={() => undefined}
    />,
  );
  expect(screen.getByText("No plans yet")).toBeVisible();
  expect(screen.getByRole("button", { name: /awaiting approval/i })).toBeVisible();
});

test("newly ready plans pop only after the first library snapshot", () => {
  const waiting = { requestId: "req-1", status: "awaiting_plan", objective: "Add retry" };
  const ready = { ...waiting, status: "accepted", planDigest: "aa" };
  const first = newlyReadyPlan(null, [waiting]);
  expect(first.discovered).toBeUndefined();
  const second = newlyReadyPlan(first.seen, [ready]);
  expect(second.discovered?.requestId).toBe("req-1");
  const third = newlyReadyPlan(second.seen, [ready]);
  expect(third.discovered).toBeUndefined();
});

test("hidden terminal panel is not in the document", () => {
  const { rerender } = render(<TerminalPanel visible={false} />);
  expect(screen.queryByLabelText("Project terminal")).toBeNull();
  rerender(<TerminalPanel visible />);
  expect(screen.getByLabelText("Project terminal")).toBeVisible();
});

test("terminal chrome looks like a shell tab and paints the 16 ANSI colors", () => {
  render(<TerminalPanel visible />);
  expect(screen.getByText("Terminal")).toBeVisible();
  expect(screen.queryByText(/cwd is the opened project/i)).toBeNull();
  expect(document.querySelector(".terminal-chrome")).not.toBeNull();
  expect(document.querySelector(".terminal-host")).not.toBeNull();
  for (const key of ANSI_THEME_KEYS) {
    expect(TERMINAL_THEME[key]).toMatch(/^#[0-9a-f]{6}$/i);
  }
  expect(TERMINAL_THEME.background).toBe("#1c1d1f");
  expect(TERMINAL_THEME.foreground).toBe("#d7dae0");
});

test("new task guide lists commander skill commands and the operator workflow", () => {
  render(
    <NewTask
      desktop={false}
      terminalVisible
      terminalSplit={0.5}
      onSplitChange={() => undefined}
      onSplitCommit={() => undefined}
      map={null}
    />,
  );
  expect(screen.getAllByText("mlx-swarm-commander").length).toBeGreaterThan(0);
  expect(screen.getAllByText("mlx-swarm-skill-map").length).toBeGreaterThan(0);
  expect(screen.getAllByText("mlx-swarm skill install --host claude").length).toBeGreaterThan(0);
  expect(screen.getAllByText("mlx-swarm skill install --host codex").length).toBeGreaterThan(0);
  expect(screen.getAllByText("mlx-swarm skill install --host claude --skills-dir .claude/skills").length).toBeGreaterThan(0);
  expect(screen.getByText("/mlx-swarm-commander")).toBeVisible();
  expect(screen.getByText("$mlx-swarm-commander")).toBeVisible();
  expect(screen.getAllByText("/mlx-swarm-skill-map").length).toBeGreaterThan(0);
  expect(screen.getByText("$mlx-swarm-skill-map")).toBeVisible();
  expect(screen.getByText("Plans")).toBeVisible();
  expect(screen.getByText("Continue")).toBeVisible();
  expect(screen.getByText(/claim-plan/)).toBeVisible();
  expect(screen.getByText(/import-plan/)).toBeVisible();
  expect(screen.getByText(/claim-review/)).toBeVisible();
  expect(screen.getByText(/never calls Claude or Codex/i)).toBeVisible();
  expect(screen.getByText(/Desktop only/i)).toBeVisible();
  expect(screen.getByRole("button", { name: "Resize terminal" })).toBeVisible();
  expect(document.querySelector(".new-task-split")).toHaveAttribute("data-split", "0.5");
  expect(document.querySelector(".new-task-split")).toHaveAttribute("data-collapsed", "false");
  expect((document.querySelector(".new-task-split") as HTMLElement).style.gridTemplateRows).toBe(
    "minmax(0, 0.5fr) 8px minmax(0, 0.5fr)",
  );
});

test("new task skill cards reflect install status from the status payload", () => {
  render(
    <SkillGuide
      skill={{
        skillName: "mlx-swarm-commander",
        hosts: [
          { id: "claude", installed: true, installCommand: "mlx-swarm skill install --host claude", invoke: "/mlx-swarm-commander" },
          { id: "codex", installed: false, installCommand: "mlx-swarm skill install --host codex", invoke: "$mlx-swarm-commander" },
        ],
      }}
    />,
  );
  expect(screen.getByText("Installed")).toBeVisible();
  expect(screen.getAllByText("Install").length).toBeGreaterThan(0);
  expect(screen.getAllByText("mlx-swarm-skill-map").length).toBeGreaterThan(0);
  expect(screen.getAllByText("/mlx-swarm-skill-map").length).toBeGreaterThan(0);
});

test("new task split keeps a provided terminal out of the skill guide", () => {
  render(
    <NewTask
      desktop
      terminalVisible
      terminalSplit={0.5}
      onSplitChange={() => undefined}
      onSplitCommit={() => undefined}
      terminal={<div data-testid="pty">pty</div>}
      map={null}
    />,
  );
  expect(screen.getByTestId("pty")).toBeVisible();
  expect(screen.queryByText(/Desktop only/i)).toBeNull();
  expect(screen.getAllByText("mlx-swarm-commander").length).toBeGreaterThan(0);
});

test("hiding the terminal on new task gives the guide the full pane", () => {
  render(
    <NewTask
      desktop
      terminalVisible={false}
      terminalSplit={0.5}
      onSplitChange={() => undefined}
      onSplitCommit={() => undefined}
      terminal={<div data-testid="pty">pty</div>}
      map={null}
    />,
  );
  expect(screen.queryByRole("button", { name: "Resize terminal" })).toBeNull();
  expect(screen.queryByTestId("pty")).toBeNull();
  expect(document.querySelector(".new-task-split")).toHaveAttribute("data-collapsed", "true");
});

test("dragging the split handle updates the clamped terminal ratio", () => {
  const splits: number[] = [];
  const { container } = render(
    <NewTask
      desktop={false}
      terminalVisible
      terminalSplit={0.5}
      onSplitChange={(value) => splits.push(value)}
      onSplitCommit={() => undefined}
      map={null}
    />,
  );
  const root = container.querySelector(".new-task-split") as HTMLElement;
  root.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    bottom: 400,
    right: 800,
    width: 800,
    height: 400,
    toJSON() {
      return {};
    },
  });
  fireEvent.mouseDown(screen.getByRole("button", { name: "Resize terminal" }));
  fireEvent.mouseMove(window, { clientY: 100 });
  expect(splits.at(-1)).toBe(0.75);
  expect(clampTerminalSplit(0.1)).toBe(0.25);
});

test("new task skill map click highlights a path without creating a plan", () => {
  const focused: string[][] = [];
  render(
    <NewTask
      desktop={false}
      terminalVisible={false}
      terminalSplit={0.5}
      onSplitChange={() => undefined}
      onSplitCommit={() => undefined}
      map={{
        workspaceRoot: "/tmp/proj",
        nodes: [
          { id: ".", path: ".", name: "proj", kind: "dir", parentId: null, fileCount: 0 },
          { id: "src", path: "src", name: "src", kind: "dir", parentId: ".", fileCount: 2 },
        ],
        edges: [{ source: ".", target: "src", kind: "contains" }],
      }}
      onFocusPaths={(paths) => focused.push(paths)}
    />,
  );
  fireEvent.click(document.querySelector('[data-node="src"]') as HTMLElement);
  expect(focused.at(-1)).toEqual(["src"]);
  expect(screen.getByText("Focus")).toBeVisible();
  expect(screen.getByRole("button", { name: "Copy path" })).toBeVisible();
});

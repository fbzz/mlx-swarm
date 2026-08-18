export type RunSummary = {
  planId: string;
  sessionId: string;
  objective?: string;
  status: string;
  reviewStatus?: string;
  startedAt?: string;
  finishedAt?: string | null;
  elapsedSeconds?: number;
  active?: boolean;
  // The API reports per-status task counts plus the two rollups the run rows
  // need. A run can be terminal with every task completed, so progress has to
  // be read from these rather than inferred from `status`.
  counts?: Record<string, number>;
  total?: number;
  completed?: number;
  pauseReason?: string | null;
};

export type CommanderRequest = {
  requestId: string;
  objective?: string;
  status: string;
  createdAt?: string;
  planDigest?: string;
  // Set once the request launches. Its work now lives as a run, so the
  // planning inbox drops it.
  sessionRef?: string | null;
};

export type PlanTask = {
  id: string;
  role?: string;
  prompt?: string;
  dependsOn?: string[];
  artifactType?: string;
  executionMode?: string;
  allowedPaths?: string[];
  verification?: string[];
  interfaceContract?: string;
  maxRepairAttempts?: number;
};

export type PlanPayload = {
  planId?: string;
  objective?: string;
  digest?: string;
  source?: string;
  levels?: string[][];
  tasks?: PlanTask[];
};

export type PlanPrompt = {
  requestId: string;
  prompt: string;
  sha256: string;
};

export type CommanderRequestDetail = {
  request: CommanderRequest & { requestId: string; status: string };
  plan: PlanPayload | null;
  validationError?: unknown;
  executionError?: string | null;
  executionPreview?: {
    ready?: boolean;
    executionDigest?: string;
    workspaceRoot?: string;
    baseSha?: string;
  } | null;
};

export type LibraryPayload = {
  activeWorkspace: {
    workspaceRoot: string;
    configPath: string;
    artifactsDir: string;
    projectRoot?: string;
  };
  workspaces: Array<{
    workspaceId: string;
    name: string;
    workspaceRoot: string;
    lastOpenedAt: string;
  }>;
  requests: CommanderRequest[];
  runs: RunSummary[];
  uiState?: {
    terminalVisible?: boolean;
    terminalSplit?: number;
    focusPaths?: string[];
  };
};

export type SkillHostStatus = {
  id: "claude" | "codex" | string;
  installed: boolean;
  installCommand: string;
  invoke: string;
};

export type SkillStatus = {
  skillName: string;
  hosts: SkillHostStatus[];
  projectClaudeCommand?: string;
};

export type SystemStatus = {
  ready: boolean;
  model: { repository: string; path?: string; error?: string };
  batch: { maxWorkers: number };
  worker: { mode: string };
  skill?: SkillStatus;
  skills?: SkillStatus[];
};

export type CodebaseMapNode = {
  id: string;
  path: string;
  name: string;
  kind: "dir" | "file" | "package" | string;
  parentId: string | null;
  fileCount?: number;
};

export type CodebaseMap = {
  workspaceRoot: string;
  truncated?: boolean;
  nodes: CodebaseMapNode[];
  edges: Array<{ source: string; target: string; kind?: string }>;
};

export type TaskState = {
  id?: string;
  role?: string;
  status: string;
  output?: string;
  normalizedOutput?: string;
  repairAttempts?: number;
  error?: string | null;
  gateResult?: {
    passed: boolean;
    violations?: Array<{ id: string; kind?: string; message?: string }>;
  };
  artifact?: { sha256?: string; artifactType?: string };
};

export type RunDetail = {
  run: RunSummary;
  plan?: PlanPayload;
  levels?: string[][];
  tasks: Record<string, TaskState>;
  artifacts?: Record<
    string,
    {
      manifest: { sha256: string; artifactType: string };
      payload: string;
      status: string;
      actions: { apply: boolean; reject: boolean; verify: boolean };
    }
  >;
  actions?: {
    resume?: boolean;
    retry?: boolean;
    review?: boolean;
    cleanupWorkspace?: boolean;
  };
  retryExecutionPreview?: { ready?: boolean; executionDigest?: string } | null;
  localUsage?: {
    promptTokens?: number;
    generationTokens?: number;
    generationCalls?: number;
  };
  frontierReview?: { verdict?: string; summary?: string };
};

export type DiffLine = {
  type: "add" | "delete" | "context";
  oldLine: number | null;
  newLine: number | null;
  text: string;
};

export type DiffPayload = {
  raw: string;
  summary: { files: number; additions: number; deletions: number };
  files: Array<{
    oldPath: string;
    newPath: string;
    status: string;
    additions: number;
    deletions: number;
    hunks: Array<{
      header: string;
      label: string;
      lines: DiffLine[];
    }>;
  }>;
};

export type AttemptPayload = {
  taskId: string;
  attempts: Array<{
    kind: string;
    attempt: number;
    phase: string;
    recordedAt?: string;
    promptSha256: string;
    outputSha256: string;
    prompt: string;
    output: string;
    authoritative: boolean;
  }>;
};

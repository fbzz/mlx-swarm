const { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, shell } = require("electron");
const path = require("node:path");
const os = require("node:os");

const appUrl = process.env.MLX_SWARM_APP_URL;
if (!appUrl) {
  throw new Error("MLX_SWARM_APP_URL is required.");
}
const expectedOrigin = new URL(appUrl).origin;

let mainWindow = null;
let ptyProcess = null;

function apiUrl(pathname) {
  return new URL(pathname, appUrl).toString();
}

async function fetchJson(pathname, init) {
  const response = await fetch(apiUrl(pathname), {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const value = await response.json();
  if (!response.ok) {
    throw new Error(value.error || `Request failed (${response.status})`);
  }
  return value;
}

async function projectRoot() {
  try {
    const library = await fetchJson("/api/library");
    return (
      library.activeWorkspace?.projectRoot
      || library.activeWorkspace?.workspaceRoot
      || process.env.MLX_SWARM_WORKSPACE_ROOT
      || process.cwd()
    );
  } catch {
    return process.env.MLX_SWARM_WORKSPACE_ROOT || process.cwd();
  }
}

function stopPty() {
  if (!ptyProcess) return;
  try {
    ptyProcess.kill();
  } catch {
    // The shell may already have exited.
  }
  ptyProcess = null;
}

function buildPtyEnvironment(source = process.env) {
  const env = { ...source };
  for (const key of [
    "TMUX", "TMUX_PANE", "STY", "WINDOW", "WINDOWID", "TERMCAP", "COLUMNS", "LINES",
    "ELECTRON_RUN_AS_NODE",
  ]) {
    delete env[key];
  }
  env.TERM = "xterm-256color";
  env.COLORTERM = "truecolor";
  env.TERM_PROGRAM = "mlx-swarm";
  env.CLICOLOR = "1";
  if (!env.LANG && !env.LC_ALL && !env.LC_CTYPE) {
    env.LANG = "en_US.UTF-8";
  }
  return env;
}

function shellArgs(shellPath) {
  const name = path.basename(shellPath).toLowerCase();
  if (name === "zsh" || name === "bash" || name === "fish") return ["-l"];
  return [];
}

async function startPty(cols = 80, rows = 24) {
  stopPty();
  let pty;
  try {
    pty = require("node-pty");
  } catch (error) {
    throw new Error(
      `The in-app terminal is unavailable (${error.message}). Run npm install.`,
    );
  }
  const cwd = await projectRoot();
  const shellPath = process.env.SHELL || (process.platform === "win32" ? "powershell.exe" : "/bin/zsh");
  ptyProcess = pty.spawn(shellPath, shellArgs(shellPath), {
    name: "xterm-256color",
    cols,
    rows,
    cwd,
    env: buildPtyEnvironment(),
    encoding: "utf8",
  });
  ptyProcess.onData((data) => {
    mainWindow?.webContents.send("terminal:data", data);
  });
  ptyProcess.onExit(() => {
    ptyProcess = null;
    mainWindow?.webContents.send("terminal:exit");
  });
}

function createWindow() {
  const window = new BrowserWindow({
    title: "MLX Swarm",
    width: 1440,
    height: 900,
    minWidth: 820,
    minHeight: 600,
    backgroundColor: "#17181a",
    show: false,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  mainWindow = window;
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
    stopPty();
  });
  window.webContents.on("will-navigate", (event, target) => {
    if (new URL(target).origin !== expectedOrigin) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "https:") void shell.openExternal(parsed.href);
    } catch {
      // Ignore malformed and model-generated links.
    }
    return { action: "deny" };
  });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => {
    callback(false);
  });
  void window.loadURL(appUrl);
}

async function openFolder() {
  const result = await dialog.showOpenDialog(mainWindow ?? undefined, {
    title: "Open project folder",
    properties: ["openDirectory", "createDirectory"],
    defaultPath: os.homedir(),
  });
  if (result.canceled || !result.filePaths[0]) return null;
  const library = await fetchJson("/api/workspace/open", {
    method: "POST",
    body: JSON.stringify({ path: result.filePaths[0] }),
  });
  stopPty();
  mainWindow?.webContents.send("workspace:changed", library);
  return library;
}

function installIpc() {
  ipcMain.handle("workspace:open", () => openFolder());
  ipcMain.handle("workspace:root", () => projectRoot());
  ipcMain.handle("terminal:start", (_event, size) => startPty(size?.cols, size?.rows));
  ipcMain.handle("terminal:write", (_event, data) => {
    if (typeof data === "string") ptyProcess?.write(data);
  });
  ipcMain.handle("terminal:resize", (_event, size) => {
    if (ptyProcess && size?.cols && size?.rows) {
      ptyProcess.resize(size.cols, size.rows);
    }
  });
  ipcMain.handle("terminal:kill", () => stopPty());
  ipcMain.handle("clipboard:writeText", (_event, text) => {
    if (typeof text === "string") clipboard.writeText(text);
  });
  ipcMain.handle("clipboard:readText", () => clipboard.readText());
}

function installMenu() {
  const cachePath = process.env.MLX_SWARM_CACHE_ROOT;
  const template = [
    {
      label: "MLX Swarm",
      submenu: [
        { role: "about" },
        { type: "separator" },
        {
          label: "Open Cache Folder",
          enabled: Boolean(cachePath),
          click: () => cachePath && void shell.openPath(path.resolve(cachePath)),
        },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "quit" },
      ],
    },
    {
      label: "File",
      submenu: [
        {
          label: "Open Folder…",
          accelerator: "CmdOrCtrl+O",
          click: () => void openFolder().catch((error) => {
            void dialog.showErrorBox("Open Folder", error.message);
          }),
        },
      ],
    },
    {
      label: "View",
      submenu: [
        {
          label: "Toggle Terminal",
          accelerator: "Ctrl+`",
          click: () => mainWindow?.webContents.send("terminal:toggle"),
        },
        { type: "separator" },
        { role: "reload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "About local data",
          click: () => void dialog.showMessageBox({
            type: "info",
            title: "Local-first by design",
            message: "Plans, prompts, artifacts, and diffs stay in this project's .mlx-swarm folder.",
          }),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.setName("MLX Swarm");
app.whenReady().then(() => {
  installIpc();
  installMenu();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});
app.on("window-all-closed", () => {
  stopPty();
  if (process.platform !== "darwin") app.quit();
});

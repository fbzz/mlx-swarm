const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  openFolder: () => ipcRenderer.invoke("workspace:open"),
  projectRoot: () => ipcRenderer.invoke("workspace:root"),
  onWorkspaceChanged: (callback) => {
    const listener = (_event, library) => callback(library);
    ipcRenderer.on("workspace:changed", listener);
    return () => ipcRenderer.removeListener("workspace:changed", listener);
  },
  terminal: {
    start: (size) => ipcRenderer.invoke("terminal:start", size),
    write: (data) => ipcRenderer.invoke("terminal:write", data),
    resize: (size) => ipcRenderer.invoke("terminal:resize", size),
    kill: () => ipcRenderer.invoke("terminal:kill"),
    copy: (text) => ipcRenderer.invoke("clipboard:writeText", text),
    paste: () => ipcRenderer.invoke("clipboard:readText"),
    onData: (callback) => {
      const listener = (_event, data) => callback(data);
      ipcRenderer.on("terminal:data", listener);
      return () => ipcRenderer.removeListener("terminal:data", listener);
    },
    onExit: (callback) => {
      const listener = () => callback();
      ipcRenderer.on("terminal:exit", listener);
      return () => ipcRenderer.removeListener("terminal:exit", listener);
    },
    onToggle: (callback) => {
      const listener = () => callback();
      ipcRenderer.on("terminal:toggle", listener);
      return () => ipcRenderer.removeListener("terminal:toggle", listener);
    },
  },
});

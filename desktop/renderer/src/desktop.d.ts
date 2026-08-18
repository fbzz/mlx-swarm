export type DesktopApi = {
  openFolder: () => Promise<unknown>;
  projectRoot: () => Promise<string>;
  onWorkspaceChanged: (callback: (library: unknown) => void) => () => void;
  terminal: {
    start: (size?: { cols?: number; rows?: number }) => Promise<void>;
    write: (data: string) => Promise<void>;
    resize: (size: { cols?: number; rows?: number }) => Promise<void>;
    kill: () => Promise<void>;
    copy: (text: string) => Promise<void>;
    paste: () => Promise<string>;
    onData: (callback: (data: string) => void) => () => void;
    onExit: (callback: () => void) => () => void;
    onToggle: (callback: () => void) => () => void;
  };
};

declare global {
  interface Window {
    desktop?: DesktopApi;
  }
}

export {};

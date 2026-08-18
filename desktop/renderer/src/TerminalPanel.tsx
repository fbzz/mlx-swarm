import { useEffect, useRef } from "react";
import { Terminal, type ITheme } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

/** 16-color + chrome palette so ANSI, ls, git, and CLIs match a dark xterm. */
export const TERMINAL_THEME: ITheme = {
  background: "#1c1d1f",
  foreground: "#d7dae0",
  cursor: "#d7dae0",
  cursorAccent: "#1c1d1f",
  selectionBackground: "#3d5a80",
  selectionForeground: "#ffffff",
  selectionInactiveBackground: "#2c3038",
  black: "#1c1d1f",
  red: "#ee5d6c",
  green: "#5fd68b",
  yellow: "#e6c07b",
  blue: "#6cb2eb",
  magenta: "#c678dd",
  cyan: "#56b6c2",
  white: "#d7dae0",
  brightBlack: "#6c6f75",
  brightRed: "#ff7b86",
  brightGreen: "#7ee0a3",
  brightYellow: "#f0d189",
  brightBlue: "#8fc7ff",
  brightMagenta: "#d89aec",
  brightCyan: "#7ad4de",
  brightWhite: "#f2f3f4",
};

export const ANSI_THEME_KEYS = [
  "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
  "brightBlack", "brightRed", "brightGreen", "brightYellow",
  "brightBlue", "brightMagenta", "brightCyan", "brightWhite",
] as const;

function isCopyChord(event: KeyboardEvent, hasSelection: boolean): boolean {
  if (event.key.toLowerCase() !== "c") return false;
  if (event.metaKey) return hasSelection;
  if (event.ctrlKey && event.shiftKey) return hasSelection;
  return Boolean(event.ctrlKey && hasSelection);
}

function isPasteChord(event: KeyboardEvent): boolean {
  const key = event.key.toLowerCase();
  if (key !== "v") return false;
  return event.metaKey || Boolean(event.ctrlKey && event.shiftKey);
}

export function TerminalPanel({ visible }: { visible: boolean }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);

  useEffect(() => {
    const desktop = window.desktop;
    const host = hostRef.current;
    if (!visible || !desktop || !host) return;
    const term = new Terminal({
      allowProposedApi: false,
      convertEol: false,
      cursorBlink: true,
      cursorStyle: "block",
      drawBoldTextInBrightColors: true,
      fontSize: 13,
      fontFamily: 'Menlo, Monaco, "SF Mono", "Geist Mono Variable", ui-monospace, monospace',
      fontWeight: "400",
      fontWeightBold: "700",
      letterSpacing: 0,
      lineHeight: 1.2,
      macOptionIsMeta: true,
      macOptionClickForcesSelection: true,
      minimumContrastRatio: 1,
      rightClickSelectsWord: true,
      scrollback: 10000,
      theme: TERMINAL_THEME,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    term.focus();
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;
    const stopData = desktop.terminal.onData((chunk) => term.write(chunk));
    const stopExit = desktop.terminal.onExit(() => {
      term.write("\r\n[terminal exited]\r\n");
    });
    term.onData((chunk) => {
      void desktop.terminal.write(chunk);
    });
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      if (isCopyChord(event, term.hasSelection())) {
        void desktop.terminal.copy(term.getSelection());
        return false;
      }
      if (isPasteChord(event)) {
        void desktop.terminal.paste().then((text) => {
          if (text) term.paste(text);
        });
        return false;
      }
      return true;
    });
    const start = () => {
      fit.fit();
      void desktop.terminal.start({ cols: term.cols, rows: term.rows }).catch((error) => {
        term.write(`\r\n${error instanceof Error ? error.message : String(error)}\r\n`);
      });
    };
    const frame = requestAnimationFrame(start);
    const observer = new ResizeObserver(() => {
      fit.fit();
      void desktop.terminal.resize({ cols: term.cols, rows: term.rows });
    });
    observer.observe(host);
    return () => {
      cancelAnimationFrame(frame);
      stopData();
      stopExit();
      observer.disconnect();
      void desktop.terminal.kill();
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, [visible]);

  if (!visible) return null;
  return (
    <section className="terminal-dock" aria-label="Project terminal">
      <header className="terminal-chrome">
        <div className="terminal-tab" aria-current="page">Terminal</div>
      </header>
      <div className="terminal-host" ref={hostRef} />
    </section>
  );
}

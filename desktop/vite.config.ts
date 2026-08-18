import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { writeFile } from "node:fs/promises";
import { fileURLToPath, URL } from "node:url";

const pythonStaticRoot = fileURLToPath(
  new URL("../src/mlx_swarm/ui_static", import.meta.url),
);

export default defineConfig({
  root: fileURLToPath(new URL("./renderer", import.meta.url)),
  plugins: [
    react(),
    tailwindcss(),
    {
      name: "python-package-marker",
      closeBundle: () => writeFile(
        `${pythonStaticRoot}/__init__.py`,
        '"""Packaged production assets for the MLX Swarm app."""\n',
      ),
    },
  ],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.MLX_SWARM_API_URL || "http://127.0.0.1:8765",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (request) => request.removeHeader("origin"));
        },
      },
    },
  },
  build: {
    outDir: pythonStaticRoot,
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: fileURLToPath(
      new URL("./renderer/src/test-setup.ts", import.meta.url),
    ),
  },
});

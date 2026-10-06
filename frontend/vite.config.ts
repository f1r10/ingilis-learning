import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // loadEnv also picks up VITE_* vars set in the shell, so VITE_PROXY_TARGET
  // still overrides the target without needing Node's `process` global here.
  const env = loadEnv(mode, ".");

  return {
    plugins: [react()],
    server: {
      host: true,
      port: 5173,
      proxy: {
        // In dev, proxy API calls to the backend so cookies work same-origin.
        "/api": {
          target: env.VITE_PROXY_TARGET || "http://localhost:8000",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: false,
    },
  };
});

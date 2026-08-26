import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const application = mode === "annotation" ? "annotation" : "capture";
  return {
    plugins: [react()],
    define: {
      __APP_KIND__: JSON.stringify(application)
    },
    build: {
      outDir: application === "annotation" ? "dist-annotation" : "dist-capture",
      emptyOutDir: true
    },
    server: {
      host: "127.0.0.1",
      port: application === "annotation" ? 5174 : 5173,
      proxy: {
        "/api": application === "annotation"
          ? "http://127.0.0.1:8766"
          : "http://127.0.0.1:8765"
      }
    }
  };
});

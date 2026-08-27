import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

function captureApiBuildId() {
  const digest = createHash("sha256");
  const root = resolve(import.meta.dirname, "../src/imu_data_collector");
  for (const name of ["capture_api.py", "coordinator.py", "models.py"]) {
    digest.update(name);
    digest.update("\0");
    digest.update(readFileSync(resolve(root, name)));
    digest.update("\0");
  }
  return digest.digest("hex").slice(0, 16);
}

export default defineConfig(({ mode }) => {
  const application = mode === "annotation" ? "annotation" : "capture";
  const apiBuildId = captureApiBuildId();
  return {
    plugins: [
      react(),
      {
        name: "imu-build-metadata",
        generateBundle() {
          this.emitFile({
            type: "asset",
            fileName: "build-meta.json",
            source: JSON.stringify({
              application,
              capture_api_build_id: apiBuildId
            }, null, 2) + "\n"
          });
        }
      }
    ],
    define: {
      __APP_KIND__: JSON.stringify(application),
      __CAPTURE_API_BUILD_ID__: JSON.stringify(apiBuildId)
    },
    build: {
      outDir: process.env.IMU_FRONTEND_OUT_DIR
        ?? (application === "annotation" ? "dist-annotation" : "dist-capture"),
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

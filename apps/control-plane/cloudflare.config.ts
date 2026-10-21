import { defineConfig } from "cf/config";

// Neither name selects a live environment. Default Vite modes are rejected too.
export default defineConfig(({ mode }) => {
  if (mode !== "offline" && mode !== "staging") {
    throw new Error("The template requires --mode offline or --mode staging.");
  }

  return {
    worker: {
      name: `irlight-control-plane-template-${mode}`,
      compatibilityDate: "2026-10-01",
      entrypoint: "./src/index.ts",
      env: {},
      triggers: [],
      domains: [],
      workersDev: false,
      previewUrls: false,
      observability: { enabled: false },
    },
  };
});

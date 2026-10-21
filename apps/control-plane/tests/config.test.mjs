import assert from "node:assert/strict";
import test from "node:test";
import config from "../cloudflare.config.ts";

for (const mode of [undefined, "", "development", "production", "prod", "unknown"]) {
  test(`configuration rejects ${String(mode)} mode`, () => {
    assert.throws(() => config({ mode, isPreview: false }), /requires --mode/);
  });
}

for (const mode of ["offline", "staging"]) {
  test(`${mode} is a complete resource-free configuration`, () => {
    const result = config({ mode, isPreview: false });
    assert.deepEqual(Object.keys(result), ["worker"]);
    assert.deepEqual(result.worker, {
      name: `irlight-control-plane-template-${mode}`,
      compatibilityDate: "2026-10-01",
      entrypoint: "./src/index.ts",
      env: {},
      triggers: [],
      domains: [],
      workersDev: false,
      previewUrls: false,
      observability: { enabled: false },
    });
  });
}

test("each mode gets independent objects and a distinct Worker name", () => {
  const local = config({ mode: "offline", isPreview: false });
  const staging = config({ mode: "staging", isPreview: false });
  assert.notEqual(local.worker.name, staging.worker.name);
  local.worker.env.modified = { type: "text", value: "offline" };
  assert.deepEqual(staging.worker.env, {});
});

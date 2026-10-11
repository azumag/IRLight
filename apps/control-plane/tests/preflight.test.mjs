import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

test("preflight rejects every missing reviewed input before invoking cf", (t) => {
  const root = mkdtempSync(join(tmpdir(), "irlight-cf-preflight-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, "scripts"));
  mkdirSync(join(root, "node_modules/.bin"), { recursive: true });
  mkdirSync(join(root, "node_modules/@cloudflare/vite-plugin"), { recursive: true });
  const script = join(root, "scripts/preflight.mjs");
  copyFileSync(new URL("../scripts/preflight.mjs", import.meta.url), script);
  const inputs = [
    "cloudflare.config.ts", "vite.config.ts", "package-lock.json",
    "node_modules/.bin/cf", "node_modules/.bin/vite", "node_modules/.bin/tsc",
    "node_modules/@cloudflare/vite-plugin/package.json",
  ];
  for (const missing of inputs) {
    for (const input of inputs) {
      const path = join(root, input);
      if (input === missing) rmSync(path, { force: true });
      else writeFileSync(path, "fixture");
    }
    const result = spawnSync(process.execPath, [script], { encoding: "utf8", timeout: 10000 });
    assert.notEqual(result.status, 0, missing);
  }
  for (const input of inputs) writeFileSync(join(root, input), "fixture");
  assert.equal(spawnSync(process.execPath, [script], { timeout: 10000 }).status, 0);
});

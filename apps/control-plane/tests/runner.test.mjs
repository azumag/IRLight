import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

test("runner passes offline environment and stops a typecheck chain on failure", (t) => {
  // All child tools are fixtures. No real cf, Vite or workerd is started here.
  const root = mkdtempSync(join(tmpdir(), "irlight-cf-runner-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const directory of [
    "scripts", "node_modules/.bin", "node_modules/cf/bin",
    "node_modules/typescript/bin", "node_modules/@cloudflare/vite-plugin",
  ]) mkdirSync(join(root, directory), { recursive: true });
  for (const name of ["run.mjs", "command-plan.mjs", "preflight.mjs"]) {
    copyFileSync(new URL(`../scripts/${name}`, import.meta.url), join(root, "scripts", name));
  }
  for (const path of [
    "cloudflare.config.ts", "vite.config.ts", "package-lock.json",
    "node_modules/.bin/cf", "node_modules/.bin/vite", "node_modules/.bin/tsc",
    "node_modules/@cloudflare/vite-plugin/package.json",
  ]) writeFileSync(join(root, path), "fixture");
  const trace = join(root, "trace.jsonl");
  const fixture = (name) => `
    require('node:fs').appendFileSync(process.env.FIXTURE_TRACE, JSON.stringify({
      tool: '${name}', args: process.argv.slice(2), telemetry: process.env.CF_SEND_TELEMETRY,
      doNotTrack: process.env.DO_NOT_TRACK, cfFetch: process.env.CLOUDFLARE_CF_FETCH_ENABLED
    }) + '\\n');
    process.exit(${name === "cf" ? "Number(process.env.FIXTURE_CF_EXIT || 0)" : "0"});
  `;
  writeFileSync(join(root, "node_modules/cf/bin/cf"), fixture("cf"));
  writeFileSync(join(root, "node_modules/typescript/bin/tsc"), fixture("tsc"));
  const run = (name, code = 0) => spawnSync(process.execPath, [join(root, "scripts/run.mjs"), name], {
    encoding: "utf8", timeout: 10000,
    env: {
      ...process.env, FIXTURE_TRACE: trace, FIXTURE_CF_EXIT: String(code),
      CF_SEND_TELEMETRY: "true", DO_NOT_TRACK: "0", CLOUDFLARE_CF_FETCH_ENABLED: "true",
    },
  });
  assert.equal(run("typecheck").status, 0);
  const rows = readFileSync(trace, "utf8").trim().split("\n").map(JSON.parse);
  assert.deepEqual(rows.map((row) => row.tool), ["cf", "tsc"]);
  for (const row of rows) {
    assert.equal(row.telemetry, "false");
    assert.equal(row.doNotTrack, "1");
    assert.equal(row.cfFetch, "false");
  }
  writeFileSync(trace, "");
  assert.equal(run("typecheck", 9).status, 9);
  assert.equal(readFileSync(trace, "utf8").trim().split("\n").length, 1);
  writeFileSync(trace, "");
  assert.equal(run("deploy").status, 1);
  assert.equal(readFileSync(trace, "utf8"), "");
});

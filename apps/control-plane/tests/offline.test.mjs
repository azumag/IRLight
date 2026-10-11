import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { commandEnvironment, commandPlan } from "../scripts/command-plan.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("local entrypoints and CI disable the upstream Request.cf fetch", () => {
  for (const path of [
    "../scripts/command-plan.mjs",
    "../../../.github/workflows/control-plane.yml",
    "./runtime/worker.test.mjs",
  ]) {
    assert.match(read(path), /CLOUDFLARE_CF_FETCH_ENABLED(?:["']?\s*[:=]\s*)["']false["']/);
  }
  const manifest = JSON.parse(read("../package.json"));
  for (const name of ["dev", "test:local", "typecheck", "build", "dry-run"]) {
    assert.equal(manifest.scripts[name], `node scripts/run.mjs ${name}`);
  }
  assert.match(read("../scripts/run.mjs"), /env: commandEnvironment\(process.env\)/);
  assert.match(read("../vite.config.ts"), /remoteBindings: false/);
  assert.match(read("../vite.config.ts"), /tunnel: false/);
});

test("every command overrides inherited telemetry and metadata-fetch opt-ins", () => {
  const inherited = { CF_SEND_TELEMETRY: "true", DO_NOT_TRACK: "0", CLOUDFLARE_CF_FETCH_ENABLED: "true" };
  assert.deepEqual(commandEnvironment(inherited), {
    CF_SEND_TELEMETRY: "false", DO_NOT_TRACK: "1", CLOUDFLARE_CF_FETCH_ENABLED: "false",
  });
  assert.equal(inherited.CF_SEND_TELEMETRY, "true");
});

test("cf plans fix modes and only expose prebuilt dry-run deployment", () => {
  assert.deepEqual(commandPlan("dev"), [["node_modules/cf/bin/cf", "dev", "--mode", "offline"]]);
  assert.deepEqual(commandPlan("typecheck"), [
    ["node_modules/cf/bin/cf", "workers", "types", "--mode", "staging"],
    ["node_modules/typescript/bin/tsc", "--noEmit"],
  ]);
  assert.deepEqual(commandPlan("build"), [["node_modules/cf/bin/cf", "build", "--mode", "staging"]]);
  assert.deepEqual(commandPlan("dry-run"), [["node_modules/cf/bin/cf", "deploy", "--prebuilt", "--mode", "staging", "--dry-run"]]);
  for (const name of [undefined, "deploy", "auth", "constructor", "__proto__"]) {
    assert.throws(() => commandPlan(name), /Unknown template command/);
  }
});

// Pure, fixed command plans. No arbitrary cf arguments or live deploy command.
export function commandPlan(name) {
  const cf = "node_modules/cf/bin/cf";
  switch (name) {
    case "dev": return [[cf, "dev", "--mode", "offline"]];
    case "typecheck": return [
      [cf, "workers", "types", "--mode", "staging"],
      ["node_modules/typescript/bin/tsc", "--noEmit"],
    ];
    case "build": return [[cf, "build", "--mode", "staging"]];
    case "dry-run": return [[cf, "deploy", "--prebuilt", "--mode", "staging", "--dry-run"]];
    // Plugin setup failure can leave handles open before a server can be closed.
    case "test:local": return [["--test", "--test-force-exit", "tests/runtime/worker.test.mjs"]];
    default: throw new Error("Unknown template command.");
  }
}

export function commandEnvironment(base) {
  return {
    ...base,
    // Fixed Request.cf placeholders; no Miniflare metadata fetch or CLI telemetry.
    CLOUDFLARE_CF_FETCH_ENABLED: "false",
    CF_SEND_TELEMETRY: "false",
    DO_NOT_TRACK: "1",
  };
}

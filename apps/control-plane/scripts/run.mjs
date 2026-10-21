import "./preflight.mjs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { commandEnvironment, commandPlan } from "./command-plan.mjs";

const project = fileURLToPath(new URL("..", import.meta.url));
for (const command of commandPlan(process.argv[2])) {
  const code = await new Promise((resolve) => {
    const child = spawn(process.execPath, command, {
      cwd: project,
      stdio: "inherit",
      env: commandEnvironment(process.env),
    });
    child.once("error", () => resolve(1));
    child.once("exit", (status) => resolve(status ?? 1));
  });
  if (code !== 0) {
    process.exitCode = code;
    break;
  }
}

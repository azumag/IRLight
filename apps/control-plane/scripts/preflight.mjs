import { accessSync, constants } from "node:fs";
import { fileURLToPath } from "node:url";

// Stop before cf can auto-configure a missing or uninstalled project.
for (const file of [
  "../cloudflare.config.ts",
  "../vite.config.ts",
  "../package-lock.json",
  "../node_modules/.bin/cf",
  "../node_modules/.bin/vite",
  "../node_modules/.bin/tsc",
  "../node_modules/@cloudflare/vite-plugin/package.json",
]) {
  accessSync(fileURLToPath(new URL(file, import.meta.url)), constants.R_OK);
}

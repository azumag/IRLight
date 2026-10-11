import { cloudflare } from "@cloudflare/vite-plugin";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [cloudflare({ inspectorPort: false, remoteBindings: false, tunnel: false, persistState: false })],
  server: { host: "127.0.0.1" },
});

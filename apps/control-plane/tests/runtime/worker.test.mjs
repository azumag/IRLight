import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";

// Enforce this even when the test file is invoked without the local runner.
process.env.CLOUDFLARE_CF_FETCH_ENABLED = "false";

test("local Workers runtime serves only the scaffold endpoints", { timeout: 60000 }, async (t) => {
  const server = await createServer({
    mode: "offline",
    server: { host: "127.0.0.1", port: 0 },
  });
  t.after(() => server.close());
  await server.listen();
  const address = server.httpServer.address();
  assert.equal(typeof address, "object");
  const origin = `http://127.0.0.1:${address.port}`;

  for (const [path, status, body] of [
    ["/healthz", 200, { status: "ok", component: "control-plane-template" }],
    ["/readyz", 503, { code: "CONTROL_PLANE_NOT_CONFIGURED" }],
    ["/v1/sessions", 404, { code: "NOT_FOUND" }],
  ]) {
    const response = await fetch(`${origin}${path}`, { signal: AbortSignal.timeout(10000) });
    assert.equal(response.status, status);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.deepEqual(await response.json(), body);
  }
  const mutation = await fetch(`${origin}/readyz`, {
    method: "POST",
    signal: AbortSignal.timeout(10000),
  });
  assert.equal(mutation.status, 405);
  assert.equal(mutation.headers.get("Allow"), "GET");
});

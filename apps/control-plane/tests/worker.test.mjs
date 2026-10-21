import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.ts";

test("health only reports template liveness, never business readiness", async () => {
  const response = worker.fetch(new Request("https://template.invalid/healthz"));
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ok", component: "control-plane-template" });
  assert.equal(response.headers.get("Cache-Control"), "no-store");
});

test("readiness fails closed", async () => {
  const response = worker.fetch(new Request("https://template.invalid/readyz"));
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { code: "CONTROL_PLANE_NOT_CONFIGURED" });
  assert.equal(response.headers.get("Cache-Control"), "no-store");
});

for (const path of ["/", "/v1/sessions", "/healthz/", "/readyz/", "/missing"]) {
  test(`unknown route ${path} is unavailable`, async () => {
    const response = worker.fetch(new Request(`https://template.invalid${path}`));
    assert.equal(response.status, 404);
    assert.deepEqual(await response.json(), { code: "NOT_FOUND" });
  });
}

for (const method of ["POST", "PUT", "DELETE", "OPTIONS", "HEAD"]) {
  test(`${method} cannot invoke health or readiness operations`, async () => {
    for (const path of ["/healthz", "/readyz"]) {
      const response = worker.fetch(new Request(`https://template.invalid${path}`, { method }));
      assert.equal(response.status, 405);
      assert.equal(response.headers.get("Allow"), "GET");
      assert.deepEqual(await response.json(), { code: "METHOD_NOT_ALLOWED" });
    }
  });
}

test("request data and credentials are not reflected", async () => {
  const response = worker.fetch(new Request("https://template.invalid/readyz?token=private-fixture", {
    headers: { Authorization: "Bearer private-fixture" },
  }));
  assert.equal(response.status, 503);
  assert.equal((await response.text()).includes("private-fixture"), false);
});

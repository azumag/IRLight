function json(body: Record<string, string>, status: number, allow?: string): Response {
  return Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      ...(allow ? { Allow: allow } : {}),
    },
  });
}

// Scaffold only: no authentication, authority, provider or media operations.
export default {
  fetch(request: Request): Response {
    const path = new URL(request.url).pathname;
    if (path !== "/healthz" && path !== "/readyz") {
      return json({ code: "NOT_FOUND" }, 404);
    }
    if (request.method !== "GET") {
      return json({ code: "METHOD_NOT_ALLOWED" }, 405, "GET");
    }
    if (path === "/healthz") {
      return json({ status: "ok", component: "control-plane-template" }, 200);
    }
    // A healthy Worker is not a configured business Control Plane.
    return json({ code: "CONTROL_PLANE_NOT_CONFIGURED" }, 503);
  },
} satisfies ExportedHandler;

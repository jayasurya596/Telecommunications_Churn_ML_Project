import { Container, getContainer } from "@cloudflare/containers";
import { createRemoteJWKSet, jwtVerify } from "jose";

export { ContainerProxy } from "@cloudflare/containers";

interface Env extends Cloudflare.Env {
  TEAM_DOMAIN?: string;
  POLICY_AUD?: string;
  ACCESS_ROLE_MAP?: string;
}

let jwksUri = "";
let accessJwks: ReturnType<typeof createRemoteJWKSet> | undefined;

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
}

function modelRow(row: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!row) return null;
  const manifest = typeof row.manifest_json === "string" ? JSON.parse(row.manifest_json) : row.manifest_json;
  return {
    model_version: row.model_version,
    run_id: row.run_id,
    dataset_version: row.dataset_version,
    state: row.state,
    manifest,
    created_at: row.created_at,
    approved_at: row.approved_at,
    approved_by: row.approved_by,
  };
}

async function registryHandler(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method.toUpperCase();

  if (method === "GET" && path === "/v1/models") {
    const result = await env.REGISTRY.prepare("SELECT * FROM models ORDER BY created_at DESC").all();
    return json((result.results ?? []).map((row) => modelRow(row as Record<string, unknown>)));
  }

  if (method === "GET" && path === "/v1/active") {
    const row = await env.REGISTRY.prepare(
      "SELECT m.* FROM models m JOIN platform_state p ON p.active_model_version = m.model_version WHERE p.id = 1",
    ).first<Record<string, unknown>>();
    return json(modelRow(row));
  }

  const modelPath = path.match(/^\/v1\/models\/(model-[a-f0-9]{16})$/);
  if (method === "GET" && modelPath) {
    const row = await env.REGISTRY.prepare("SELECT * FROM models WHERE model_version = ?")
      .bind(modelPath[1]).first<Record<string, unknown>>();
    return row ? json(modelRow(row)) : json({ error: "model_not_found" }, 404);
  }

  if (method === "PUT" && modelPath) {
    const body = await request.json() as { manifest?: Record<string, unknown> };
    const manifest = body.manifest;
    if (!manifest || manifest.model_version !== modelPath[1]) return json({ error: "invalid_manifest" }, 400);
    const gates = manifest.release_gates as Record<string, unknown> | undefined;
    const createdAt = String(manifest.created_at ?? new Date().toISOString());
    try {
      await env.REGISTRY.prepare(
        "INSERT INTO models(model_version, run_id, dataset_version, schema_version, state, manifest_json, created_at, candidate_count, release_gates_pass) VALUES (?, ?, ?, ?, 'pending_approval', ?, ?, ?, ?)",
      ).bind(
        modelPath[1], String(manifest.run_id), String(manifest.dataset_version), String(manifest.schema_version),
        JSON.stringify(manifest), createdAt, Number(manifest.candidate_count ?? 0),
        gates?.automated_gates_pass === true ? 1 : 0,
      ).run();
    } catch {
      return json({ error: "model_already_registered_or_invalid" }, 409);
    }
    const row = await env.REGISTRY.prepare("SELECT * FROM models WHERE model_version = ?")
      .bind(modelPath[1]).first<Record<string, unknown>>();
    return row ? json(modelRow(row), 201) : json({ error: "model_registration_failed" }, 500);
  }

  if (method === "POST" && path === "/v1/approval") {
    const body = await request.json() as {
      model_version?: string; decision?: string; reason?: string; actor?: string;
    };
    const { model_version: version, decision, reason, actor } = body;
    if (!version || !decision || !reason || !actor) return json({ error: "invalid_approval_request" }, 400);
    const candidate = await env.REGISTRY.prepare("SELECT * FROM models WHERE model_version = ?")
      .bind(version).first<Record<string, unknown>>();
    if (!candidate) return json({ error: "model_not_found" }, 404);
    const current = await env.REGISTRY.prepare("SELECT active_model_version FROM platform_state WHERE id = 1")
      .first<{ active_model_version: string | null }>();
    const previous = current?.active_model_version ?? null;
    const timestamp = new Date().toISOString();
    const candidateState = String(candidate.state);

    if (decision === "approve") {
      if (candidateState !== "pending_approval" && candidateState !== "approved") {
        return json({ error: "candidate_not_approvable" }, 409);
      }
      if (Number(candidate.release_gates_pass) !== 1) return json({ error: "release_gates_not_met" }, 409);
      await env.REGISTRY.batch([
        env.REGISTRY.prepare("UPDATE models SET state='approved', approved_at=?, approved_by=? WHERE model_version=?")
          .bind(timestamp, actor, version),
        env.REGISTRY.prepare("UPDATE platform_state SET active_model_version=? WHERE id=1").bind(version),
        env.REGISTRY.prepare(
          "INSERT INTO approval_events(event_id, model_version, actor, decision, reason, previous_model_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ).bind(crypto.randomUUID(), version, actor, decision, reason, previous, timestamp),
      ]);
    } else if (decision === "reject") {
      if (candidateState !== "pending_approval") return json({ error: "candidate_not_pending" }, 409);
      await env.REGISTRY.batch([
        env.REGISTRY.prepare("UPDATE models SET state='rejected' WHERE model_version=?").bind(version),
        env.REGISTRY.prepare(
          "INSERT INTO approval_events(event_id, model_version, actor, decision, reason, previous_model_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ).bind(crypto.randomUUID(), version, actor, decision, reason, previous, timestamp),
      ]);
    } else if (decision === "rollback") {
      if (candidateState !== "approved") return json({ error: "rollback_target_not_approved" }, 409);
      if (Number(candidate.release_gates_pass) !== 1) return json({ error: "release_gates_not_met" }, 409);
      await env.REGISTRY.batch([
        env.REGISTRY.prepare("UPDATE platform_state SET active_model_version=? WHERE id=1").bind(version),
        env.REGISTRY.prepare(
          "INSERT INTO approval_events(event_id, model_version, actor, decision, reason, previous_model_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ).bind(crypto.randomUUID(), version, actor, decision, reason, previous, timestamp),
      ]);
    } else {
      return json({ error: "unsupported_decision" }, 400);
    }
    return json({ model_version: version, decision, reason, actor, previous_model_version: previous, timestamp });
  }

  if (method === "POST" && path === "/v1/events") {
    const body = await request.json() as Record<string, unknown>;
    await env.REGISTRY.prepare(
      "INSERT INTO api_events(event_id, path, method, status_code, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    ).bind(
      crypto.randomUUID(), String(body.path).slice(0, 256), String(body.method).slice(0, 12),
      Number(body.status_code), body.actor ? String(body.actor) : null, String(body.created_at),
    ).run();
    return json({ ok: true }, 201);
  }

  if (method === "GET" && path === "/v1/metrics") {
    const month = new Date().toISOString().slice(0, 7);
    const row = await env.REGISTRY.prepare(
      "SELECT COUNT(*) AS total, SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS errors FROM api_events WHERE substr(created_at, 1, 7) = ?",
    ).bind(month).first<{ total: number; errors: number | null }>();
    return json({ request_count: Number(row?.total ?? 0), error_count: Number(row?.errors ?? 0) });
  }

  if (method === "GET" && path === "/v1/quota") {
    const month = new Date().toISOString().slice(0, 7);
    const limit = Number.parseInt(env.MAX_REQUESTS_PER_MONTH ?? "5000", 10);
    const row = await env.REGISTRY.prepare("SELECT requests FROM monthly_usage WHERE month = ?")
      .bind(month).first<{ requests: number }>();
    const requests = Number(row?.requests ?? 0);
    return json({ month, limit, requests, remaining: Math.max(0, limit - requests) });
  }

  if (method === "POST" && path === "/v1/quota/consume") {
    return consumeQuota(env);
  }

  return json({ error: "registry_route_not_found" }, 404);
}

async function artifactHandler(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const key = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
  if (!key.startsWith("models/") || key.includes("..") || key.length > 512) {
    return new Response("Not found", { status: 404 });
  }
  if (request.method === "GET") {
    const object = await env.MODEL_ARTIFACTS.get(key);
    if (!object) return new Response("Not found", { status: 404 });
    return new Response(object.body, {
      headers: { "Content-Type": object.httpMetadata?.contentType ?? "application/octet-stream" },
    });
  }
  if (request.method === "PUT") {
    await env.MODEL_ARTIFACTS.put(key, request.body, {
      httpMetadata: { contentType: request.headers.get("content-type") ?? "application/octet-stream" },
    });
    return new Response(null, { status: 204 });
  }
  return new Response("Method not allowed", { status: 405 });
}

export class ChurnApiContainer extends Container<Env> {
  defaultPort = 8000;
  sleepAfter = "2m";
  pingEndpoint = "localhost/health/live";
  enableInternet = false;
  envVars: Record<string, string> = {
    APP_ENV: "production",
    AUTH_MODE: "access_headers",
    STORE_MODE: "cloudflare",
    MAX_REQUESTS_PER_MONTH: "5000",
    MAX_MODEL_ARTIFACT_BYTES: "4194304",
    CHURN_MODEL_DIR: "/tmp/churn-models",
    CHURN_ARTIFACTS_DIR: "/tmp/churn-artifacts",
  };
}

ChurnApiContainer.outboundByHost = {
  "registry.internal": (request: Request, env: Env) => registryHandler(request, env),
  "models.r2": (request: Request, env: Env) => artifactHandler(request, env),
};

async function consumeQuota(env: Env): Promise<Response> {
  const month = new Date().toISOString().slice(0, 7);
  const limit = Number.parseInt(env.MAX_REQUESTS_PER_MONTH ?? "5000", 10);
  if (!Number.isFinite(limit) || limit < 1) return json({ error: "quota_not_configured" }, 503);
  const row = await env.REGISTRY.prepare(
    "INSERT INTO monthly_usage(month, requests) VALUES (?, 1) " +
      "ON CONFLICT(month) DO UPDATE SET requests = requests + 1 WHERE requests < ? RETURNING requests",
  ).bind(month, limit).first<{ requests: number }>();
  if (!row) return json({ error: "monthly_request_quota_exceeded", month, limit }, 429);
  return json({ month, limit, requests: Number(row.requests), remaining: Math.max(0, limit - Number(row.requests)) });
}

async function authenticate(request: Request, env: Env): Promise<{ email: string; role: string } | Response> {
  if (String(env.APP_ENV) === "local") return { email: "local-demo@localhost", role: "admin" };
  if (!env.TEAM_DOMAIN || !env.POLICY_AUD || !env.ACCESS_ROLE_MAP) {
    return json({ error: "access_not_configured" }, 503);
  }
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return json({ error: "authentication_required" }, 401);
  try {
    const teamDomain = new URL(env.TEAM_DOMAIN).origin;
    const uri = `${teamDomain}/cdn-cgi/access/certs`;
    if (uri !== jwksUri || !accessJwks) {
      jwksUri = uri;
      accessJwks = createRemoteJWKSet(new URL(uri));
    }
    const { payload } = await jwtVerify(token, accessJwks, { issuer: teamDomain, audience: env.POLICY_AUD });
    const email = typeof payload.email === "string" ? payload.email.toLowerCase() : "";
    const roles = JSON.parse(env.ACCESS_ROLE_MAP) as Record<string, string>;
    const role = roles[email];
    if (!role || !["analyst", "ml_operator", "approver", "admin"].includes(role)) {
      return json({ error: "project_access_not_granted" }, 403);
    }
    return { email, role };
  } catch {
    return json({ error: "invalid_access_token" }, 401);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const identity = await authenticate(request, env);
    if (identity instanceof Response) return identity;

    const headers = new Headers(request.headers);
    headers.delete("x-platform-user");
    headers.delete("x-platform-role");
    headers.delete("x-demo-role");
    headers.delete("cf-access-jwt-assertion");
    headers.set("x-platform-user", identity.email);
    headers.set("x-platform-role", identity.role);

    if (url.pathname === "/health/live" || url.pathname === "/health/ready") {
      if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);
      return getContainer(env.CHURN_API, "churn-api").fetch(new Request(request, { headers }));
    }
    if (url.pathname.startsWith("/v1/")) {
      let quotaState: { limit: number; remaining: number } | undefined;
      if (!(url.pathname === "/v1/quota" && request.method === "GET")) {
        const quota = await consumeQuota(env);
        if (!quota.ok) return quota;
        quotaState = await quota.json() as { limit: number; remaining: number };
      }
      const apiResponse = await getContainer(env.CHURN_API, "churn-api").fetch(new Request(request, { headers }));
      const responseHeaders = new Headers(apiResponse.headers);
      if (quotaState) {
        responseHeaders.set("X-Request-Quota-Limit", String(quotaState.limit));
        responseHeaders.set("X-Request-Quota-Remaining", String(quotaState.remaining));
      }
      return new Response(apiResponse.body, {
        status: apiResponse.status,
        statusText: apiResponse.statusText,
        headers: responseHeaders,
      });
    }
    return env.ASSETS.fetch(new Request(request, { headers }));
  },
};

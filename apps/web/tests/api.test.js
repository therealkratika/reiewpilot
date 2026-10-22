import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  approveAndPublishFix,
  getCurrentUser,
  logout,
  publishFix,
  runReviewPipeline,
} from "../src/services/api.js";

const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalDocument === undefined) {
    delete globalThis.document;
  } else {
    globalThis.document = originalDocument;
  }
});

function response({ ok, status = 400, contentType = "application/json", body }) {
  return {
    ok,
    status,
    headers: { get: () => contentType },
    json: async () => {
      if (body instanceof Error) throw body;
      return body;
    },
    text: async () => String(body ?? ""),
  };
}

test("review API displays backend JSON error details", async () => {
  globalThis.fetch = async () => response({
    ok: false,
    body: { detail: "GitHub token lacks permission." },
  });

  await assert.rejects(
    runReviewPipeline({ owner: "acme", repo: "widgets", pullNumber: 7 }),
    { message: "GitHub token lacks permission." },
  );
});

test("fix API handles non-JSON backend errors", async () => {
  globalThis.fetch = async () => response({
    ok: false,
    contentType: "text/plain",
    body: "Service unavailable",
  });

  await assert.rejects(
    publishFix({
      approvalId: 7,
    }),
    { message: "Service unavailable" },
  );
});

test("expired sessions are reported as signed out", async () => {
  globalThis.fetch = async () => response({ ok: false, status: 401, body: { detail: "Session expired" } });
  assert.equal(await getCurrentUser(), null);
});

test("logout invalidates the session with the CSRF header", async () => {
  globalThis.document = { cookie: "reviewpilot_csrf=csrf-token" };
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return response({ ok: true, body: { authenticated: false } });
  };
  await logout();
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.credentials, "include");
  assert.equal(request.options.headers.get("X-CSRF-Token"), "csrf-token");
});

test("fix approval uses the server-issued record before publication", async () => {
  const requests = [];
  globalThis.document = { cookie: "reviewpilot_csrf=csrf-token" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? response({ ok: true, body: { approval_id: 42 } })
      : response({ ok: true, body: { status: "published", published: true } });
  };
  const result = await approveAndPublishFix(9);
  assert.equal(result.published, true);
  assert.match(requests[0].url, /\/v1\/github\/fixes\/9\/approve$/);
  assert.match(requests[1].url, /\/v1\/github\/fixes\/publish$/);
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    approval_id: 42,
    dry_run: false,
  });
  assert.equal("approved" in JSON.parse(requests[1].options.body), false);
});

test("review API reports invalid JSON error responses clearly", async () => {
  globalThis.fetch = async () => response({
    ok: false,
    body: new SyntaxError("invalid JSON"),
  });

  await assert.rejects(
    runReviewPipeline({ owner: "acme", repo: "widgets", pullNumber: 7 }),
    { message: "The API returned an invalid JSON response." },
  );
});

test("review API translates network failures into actionable errors", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("connection refused");
  };

  await assert.rejects(
    runReviewPipeline({ owner: "acme", repo: "widgets", pullNumber: 7 }),
    { message: "Unable to reach the ReviewPilot API. Is the backend running?" },
  );
});

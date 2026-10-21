import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { publishFix, runReviewPipeline } from "../src/services/api.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
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
      owner: "acme",
      repo: "widgets",
      pullNumber: 7,
      expectedHeadSha: "abc",
      filePath: "src/index.js",
      updatedCode: "new code",
    }),
    { message: "Service unavailable" },
  );
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

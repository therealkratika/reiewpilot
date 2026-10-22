const apiEnvironment = import.meta.env ?? {};
const apiUrl = (
  apiEnvironment.VITE_API_URL
  ?? apiEnvironment.VITE_AGENTS_URL
  ?? "http://localhost:8000"
).replace(/\/+$/, "");

export const apiBaseUrl = apiUrl;

function csrfToken() {
  if (typeof document === "undefined") return "";
  const match = document.cookie.match(/(?:^|;\s*)reviewpilot_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function apiFetch(url, options = {}) {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers ?? {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", csrfToken());
  }
  const response = await fetch(url, {
    ...options,
    method,
    headers,
    credentials: "include",
  });
  if (
    response.status === 401
    && !url.endsWith("/v1/auth/me")
    && typeof window !== "undefined"
  ) {
    window.dispatchEvent(new Event("reviewpilot:session-expired"));
  }
  return response;
}

async function readResponseBody(response) {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return { detail: "The API returned an invalid JSON response." };
    }
  }

  const text = await response.text();
  return { detail: text || "The API returned an empty error response." };
}

export async function runReviewPipeline({
  owner,
  repo,
  pullNumber,
  proposeFix = false,
  validateFix = false,
  targetFile = null,
  testFiles = {},
  maxFindings = 10,
}) {
  if (!Number.isInteger(pullNumber) || pullNumber < 1) {
    throw new Error("Pull request number must be a positive integer.");
  }

  let response;
  try {
    response = await apiFetch(
      `${apiUrl}/v1/github/pulls/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/${pullNumber}/review-pipeline`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          propose_fix: proposeFix,
          validate_fix: validateFix,
          target_file: targetFile,
          test_files: testFiles,
          max_findings: maxFindings,
        }),
      },
    );
  } catch {
    throw new Error("Unable to reach the ReviewPilot API. Is the backend running?");
  }

  const body = await readResponseBody(response);
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "The review request failed.";
    throw new Error(detail);
  }
  return body;
}

export const runReview = runReviewPipeline;

export async function publishFix({ approvalId, dryRun = true }) {
  let response;
  try {
    response = await apiFetch(`${apiUrl}/v1/github/fixes/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approval_id: approvalId,
        dry_run: dryRun,
      }),
    });
  } catch {
    throw new Error("Unable to reach the ReviewPilot API. Is the backend running?");
  }

  const body = await readResponseBody(response);
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "The fix publication failed.";
    throw new Error(detail);
  }
  return body;
}

export async function approveFix(fixProposalId) {
  let response;
  try {
    response = await apiFetch(`${apiUrl}/v1/github/fixes/${fixProposalId}/approve`, {
      method: "POST",
    });
  } catch {
    throw new Error("Unable to reach the ReviewPilot API. Is the backend running?");
  }
  const body = await readResponseBody(response);
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "The fix approval failed.");
  }
  return body;
}

export async function approveAndPublishFix(fixProposalId) {
  const approval = await approveFix(fixProposalId);
  return publishFix({ approvalId: approval.approval_id, dryRun: false });
}

export async function getCurrentUser() {
  let response;
  try {
    response = await apiFetch(`${apiUrl}/v1/auth/me`);
  } catch {
    throw new Error("Unable to reach the ReviewPilot API. Is the backend running?");
  }
  if (response.status === 401) return null;
  const body = await readResponseBody(response);
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Could not verify the session.");
  }
  return body.user;
}

export async function logout() {
  let response;
  try {
    response = await apiFetch(`${apiUrl}/v1/auth/logout`, { method: "POST" });
  } catch {
    throw new Error("Unable to reach the ReviewPilot API. Is the backend running?");
  }
  const body = await readResponseBody(response);
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Logout failed.");
  }
  return body;
}

export async function getReviewHistory() {
  let response;
  try {
    response = await apiFetch(`${apiUrl}/v1/reviews/history`);
  } catch {
    throw new Error("Unable to reach the ReviewPilot API. Is the backend running?");
  }
  const body = await readResponseBody(response);
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Could not load review history.");
  }
  return body.reviews;
}

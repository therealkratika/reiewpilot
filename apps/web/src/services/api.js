const apiUrl = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

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
    response = await fetch(
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

export async function publishFix({
  owner,
  repo,
  pullNumber,
  expectedHeadSha,
  filePath,
  updatedCode,
  commitMessage,
  dryRun = false,
  approved = false,
}) {
  let response;
  try {
    response = await fetch(`${apiUrl}/v1/github/fixes/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        owner,
        repo,
        pull_number: pullNumber,
        expected_head_sha: expectedHeadSha,
        file_path: filePath,
        updated_code: updatedCode,
        commit_message: commitMessage,
        dry_run: dryRun,
        approved,
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

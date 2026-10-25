import { useState } from "react";
import ReviewForm from "../components/ReviewForm";
import { publishFix, runReviewPipeline } from "../services/api";

function StatCard({ label, value, detail, tone = "" }) {
  return (
    <article className={`stat-card ${tone}`}>
      <span className="stat-label">{label}</span>
      <strong>{value}</strong>
      <span className="stat-detail">{detail}</span>
    </article>
  );
}

function SeverityBadge({ severity }) {
  return <span className={`severity severity-${severity}`}>{severity}</span>;
}

function FindingsSection({ review }) {
  const findings = review?.findings ?? [];

  return (
    <section className="panel findings-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Review output</span>
          <h2>Findings</h2>
        </div>
        <span className="count-badge">{findings.length}</span>
      </div>
      {findings.length === 0 ? (
        <div className="empty-state compact">
          <span className="empty-icon">✓</span>
          <div>
            <strong>No actionable findings</strong>
            <p>The review did not return any findings for the changed lines.</p>
          </div>
        </div>
      ) : (
        <div className="findings-list">
          {findings.map((finding, index) => (
            <article className="finding" key={`${finding.file_path}-${finding.line_start}-${index}`}>
              <div className="finding-topline">
                <SeverityBadge severity={finding.severity} />
                <span className="finding-category">{finding.category}</span>
                <span className="finding-confidence">{Math.round(finding.confidence * 100)}% confidence</span>
              </div>
              <h3>{finding.title}</h3>
              <div className="file-location">
                <span>⌁</span>
                <code>{finding.file_path}</code>
                <span>lines {finding.line_start}–{finding.line_end}</span>
              </div>
              <p>{finding.description}</p>
              <div className="finding-evidence">
                <strong>Suggested fix</strong>
                <span>{finding.suggested_fix}</span>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function FixSection({ fix, onPublish, publishing, publishResult }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Optional output</span>
          <h2>Proposed fix</h2>
        </div>
        {fix?.status && <span className="status-pill">{fix.status.replaceAll("_", " ")}</span>}
      </div>
      {!fix ? (
        <div className="unavailable">Fix proposals were not requested for this review.</div>
      ) : (
        <>
          <p className="panel-copy">{fix.explanation ?? "No explanation was returned by the backend."}</p>
          <div className="code-grid">
            <div>
              <div className="code-heading"><span>Original code</span><span className="muted">Unavailable</span></div>
              <pre className="code-block unavailable-code">The pipeline response did not include the original file contents.</pre>
            </div>
            <div>
              <div className="code-heading"><span>Suggested patch</span><code>{fix.file_path ?? "Target file unavailable"}</code></div>
              <pre className="code-block patch-block">{fix.patch || "No patch was returned."}</pre>
            </div>
          </div>
          {fix.status === "proposal_ready" && fix.updated_code && (
            <div className="publish-action">
              <div><strong>Human approval required</strong><span>Review the patch above before creating a commit on the PR branch.</span></div>
              <button className="primary-button" onClick={onPublish} disabled={publishing}>
                {publishing ? "Publishing..." : "Approve & publish fix"}
              </button>
            </div>
          )}
          {publishResult && (
            <div className={`publish-result ${publishResult.published ? "publish-success" : "publish-warning"}`}>
              <strong>{publishResult.published ? "Fix published" : publishResult.status.replaceAll("_", " ")}</strong>
              <span>{publishResult.commit_url ? <a href={publishResult.commit_url} target="_blank" rel="noreferrer">View commit ↗</a> : publishResult.message}</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ValidationSection({ validation }) {
  const syntax = validation?.syntax_check;
  const tests = validation?.tests;
  const outcome = validation?.status ?? "not_attempted";
  const stateLabel = outcome === "passed" ? "Passed" : outcome === "failed" ? "Failed" : "Not run";

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Safety checks</span>
          <h2>Validation</h2>
        </div>
        <span className={`status-pill status-${outcome}`}>{stateLabel}</span>
      </div>
      {!validation || outcome === "not_attempted" ? (
        <div className="unavailable">{validation?.reason ?? "Validation was not requested for this review."}</div>
      ) : (
        <div className="validation-grid">
          <div className="validation-item">
            <span className={`check-dot ${syntax?.status === "passed" ? "success" : ""}`}>●</span>
            <div><strong>Syntax check</strong><span>{syntax?.status ?? "Unavailable"}</span></div>
          </div>
          <div className="validation-item">
            <span className={`check-dot ${tests?.status === "passed" ? "success" : ""}`}>●</span>
            <div><strong>Node tests</strong><span>{tests?.status ?? "Not run"}</span></div>
          </div>
        </div>
      )}
    </section>
  );
}

export default function Dashboard() {
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishResult, setPublishResult] = useState(null);

  async function submit(request) {
    setBusy(true);
    setError("");
    try {
      setResult(await runReviewPipeline(request));
      setPublishResult(null);
    } catch (requestError) {
      setResult(null);
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function publishAcceptedFix() {
    const fix = result?.fix;
    const pullRequest = result?.pull_request;
    if (!fix?.updated_code || !fix.file_path || !pullRequest?.head_sha) return;
    if (!window.confirm("Create a commit with this fix on the pull request branch? This cannot be undone automatically.")) return;
    setPublishing(true);
    setPublishResult(null);
    try {
      const [owner, repo] = (pullRequest.repository ?? "").split("/");
      setPublishResult(await publishFix({
        owner,
        repo,
        pullNumber: pullRequest.pull_number,
        expectedHeadSha: pullRequest.head_sha,
        filePath: fix.file_path,
        updatedCode: fix.updated_code,
        commitMessage: `Apply ReviewPilot fix: ${fix.file_path}`,
        approved: true,
      }));
    } catch (publishError) {
      setPublishResult({ published: false, status: "failed", message: publishError.message });
    } finally {
      setPublishing(false);
    }
  }

  const findings = result?.review?.findings ?? [];
  const criticalHigh = findings.filter((finding) => ["critical", "high"].includes(finding.severity)).length;
  const validationStatus = result?.validation?.status ?? "not run";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">R</span><span>ReviewPilot</span></div>
        <nav className="sidebar-nav" aria-label="Primary navigation">
          <a className="nav-item active" href="#review"><span>◈</span>Pull request review</a>
          <a className="nav-item" href="#history"><span>◷</span>Review history</a>
          <a className="nav-item" href="#settings"><span>⚙</span>Settings</a>
        </nav>
        <div className="sidebar-footer"><span className="status-dot" />Agent service connected</div>
      </aside>
      <main className="main-content" id="review">
        <header className="page-header">
          <div>
            <span className="eyebrow">Developer workspace</span>
            <h1>Pull request review</h1>
            <p>Find risks before they reach production. Review changed code and inspect safe fix proposals.</p>
          </div>
          {result?.pull_request?.html_url && (
            <a className="external-link" href={result.pull_request.html_url} target="_blank" rel="noreferrer">Open on GitHub ↗</a>
          )}
        </header>

        <section className="panel request-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">Start a review</span><h2>GitHub pull request</h2></div>
            <span className="step-label">Step 1 of 1</span>
          </div>
          <ReviewForm onSubmit={submit} busy={busy} />
        </section>

        {busy && <div className="loading-state"><span className="spinner" /><div><strong>Reviewing pull request...</strong><span>Fetching the diff and analyzing changed lines.</span></div></div>}
        {error && <div className="error-banner" role="alert"><strong>Review failed</strong><span>{error}</span></div>}

        {!result && !busy && !error && (
          <div className="empty-state welcome-state"><span className="empty-icon">✦</span><div><strong>Your review workspace is ready</strong><p>Enter a repository and pull request number above to see AI-assisted findings here.</p></div></div>
        )}

        {result && !busy && (
          <>
            <div className="review-context">
              <div><span className="repo-name">{result.pull_request?.repository ?? "Repository unavailable"}</span><span className="pr-title">{result.pull_request?.title ?? "Pull request title unavailable"}</span></div>
              <span className={`status-pill status-${result.status}`}>{result.status?.replaceAll("_", " ")}</span>
            </div>
            <div className="stats-grid">
              <StatCard label="Total findings" value={findings.length} detail={`${result.review?.rejected_findings ?? 0} rejected by validation`} />
              <StatCard label="Critical & high" value={criticalHigh} detail="Requires attention" tone={criticalHigh ? "stat-warning" : ""} />
              <StatCard label="Validation" value={validationStatus.replaceAll("_", " ")} detail={result.validation?.reason ?? "Fix validation result"} tone={validationStatus === "passed" ? "stat-success" : ""} />
            </div>
            {result.review?.summary && <div className="summary-callout"><span className="summary-icon">✦</span><div><strong>Review summary</strong><p>{result.review.summary}</p></div></div>}
            <FindingsSection review={result.review} />
            <FixSection fix={result.fix} onPublish={publishAcceptedFix} publishing={publishing} publishResult={publishResult} />
            <ValidationSection validation={result.validation} />
          </>
        )}
      </main>
    </div>
  );
}

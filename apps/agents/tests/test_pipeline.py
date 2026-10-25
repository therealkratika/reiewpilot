from app.pipeline import run_github_review_pipeline
from app.schemas import Finding, GitHubPipelineInput, ReviewAnalysis


class FakeGitHub:
    def get_pull_request(self, owner, repo, number):
        return {"title":"Sample", "state":"open", "draft":False, "html_url":"https://example.invalid/pr/1",
                "base":{"sha":"base123"}, "head":{"sha":"head12345", "ref":"feature"}}
    def get_pull_diff(self, owner, repo, number):
        return "diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n@@ -1,1 +1,2 @@\n const a = 1;\n+const b = 2;\n"
    def get_pull_files(self, owner, repo, number):
        return [{"filename":"src/a.js","status":"modified","additions":1,"deletions":0}]


def test_pipeline_rejects_closed_pr_without_calling_model(monkeypatch):
    from app import pipeline
    client = FakeGitHub()
    original = client.get_pull_request
    def closed(owner, repo, number):
        return {"title":"Sample","state":"closed","head":{"sha":"head12345"}}
    client.get_pull_request = closed
    result = run_github_review_pipeline("owner", "repo", 1, GitHubPipelineInput(), client)
    assert result["status"] == "rejected"


def test_pipeline_returns_current_head_and_only_added_line_findings(monkeypatch):
    from app import pipeline

    client = FakeGitHub()
    finding = Finding(
        category="bug",
        severity="high",
        title="Unsafe change",
        description="This change can fail for a concrete input.",
        evidence="The new value is used without a guard.",
        suggested_fix="Validate the value before using it.",
        file_path="src/a.js",
        line_start=2,
        line_end=2,
        confidence=0.9,
    )
    monkeypatch.setattr(
        pipeline,
        "analyze_diff",
        lambda _: ReviewAnalysis(summary="Found one issue.", findings=[finding]),
    )

    result = run_github_review_pipeline("owner", "repo", 1, GitHubPipelineInput(), client)

    assert result["pull_request"]["head_sha"] == "head12345"
    assert result["review"]["findings"][0]["line_start"] == 2

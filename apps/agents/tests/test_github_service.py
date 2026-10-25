import pytest

from app.github_client import GitHubAPIError
from app.github_service import publish_fix, publish_review
from app.schemas import GitHubFixPublishInput, GitHubPublishInput


class FakeGitHub:
    def __init__(self, sha="abcdef1234567"):
        self.sha, self.posted = sha, None
    def get_pull_request(self, owner, repo, number):
        return {"head": {"sha": self.sha}}
    def get_pull_diff(self, owner, repo, number):
        return "diff --git a/src/example.js b/src/example.js\n--- a/src/example.js\n+++ b/src/example.js\n@@ -1,1 +1,2 @@\n const a = 1;\n+const b = 2;\n"
    def create_review(self, owner, repo, number, body, event, comments):
        self.posted = comments
        return {"id": 1, "html_url": "https://example.invalid/review"}


def test_dry_run_does_not_publish():
    client = FakeGitHub()
    payload = GitHubPublishInput(owner="o", repo="r", pull_number=1, expected_head_sha=client.sha,
        summary="Review", dry_run=True, comments=[{"path":"src/example.js","line":2,"body":"Check this."}])
    assert publish_review(payload, client)["status"] == "dry_run"
    assert client.posted is None


class FakeFixGitHub:
    def __init__(self, state="open", sha="abcdef1234567"):
        self.state, self.sha, self.commits = state, sha, []

    def get_pull_request(self, owner, repo, number):
        return {"state": self.state, "head": {"sha": self.sha, "ref": "feature"}}

    def get_pull_files(self, owner, repo, number):
        return [{"filename": "src/example.js"}]

    def create_commit_on_branch(self, *args):
        self.commits.append(args)
        return {"oid": "newsha123", "url": "https://example.invalid/commit/newsha123"}


def fix_payload(**overrides):
    values = {
        "owner": "o",
        "repo": "r",
        "pull_number": 1,
        "expected_head_sha": "abcdef1234567",
        "file_path": "src/example.js",
        "updated_code": "const answer = 42;\n",
        "dry_run": False,
        "approved": True,
    }
    values.update(overrides)
    return GitHubFixPublishInput(**values)


def test_fix_publish_rejects_stale_head_sha():
    assert publish_fix(fix_payload(), FakeFixGitHub(sha="different123"))["status"] == "stale_head_sha"


def test_fix_publish_rejects_closed_pr():
    assert publish_fix(fix_payload(), FakeFixGitHub(state="closed"))["status"] == "closed_pr"


def test_fix_publish_rejects_file_outside_current_pr():
    client = FakeFixGitHub()
    client.get_pull_files = lambda *args: [{"filename": "src/other.js"}]
    assert publish_fix(fix_payload(), client)["status"] == "invalid_file_path"


def test_fix_publish_dry_run_does_not_create_commit():
    client = FakeFixGitHub()
    assert publish_fix(fix_payload(dry_run=True), client)["status"] == "dry_run"
    assert client.commits == []


def test_fix_publish_requires_explicit_approval():
    client = FakeFixGitHub()
    result = publish_fix(fix_payload(approved=False), client)
    assert result["status"] == "approval_required"
    assert client.commits == []


def test_fix_publish_creates_one_file_commit():
    client = FakeFixGitHub()
    result = publish_fix(fix_payload(), client)
    assert result["status"] == "published"
    assert result["commit_sha"] == "newsha123"
    assert client.commits[0][3:5] == ("abcdef1234567", "src/example.js")


def test_fix_publish_reports_missing_write_permission():
    class ForbiddenClient(FakeFixGitHub):
        def create_commit_on_branch(self, *args):
            raise GitHubAPIError(403, "GitHub token lacks permission to create commits.")

    with pytest.raises(GitHubAPIError, match="lacks permission"):
        publish_fix(fix_payload(), ForbiddenClient())

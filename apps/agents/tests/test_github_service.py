import pytest

from app.github_client import GitHubAPIError
from app.github_service import _review_idempotency_marker, publish_fix, publish_review
from app.schemas import GitHubFixPublishInput, GitHubPublishInput


class FakeGitHub:
    def __init__(self, sha="abcdef1234567"):
        self.sha, self.posted, self.commit_id = sha, None, None
    def get_pull_request(self, owner, repo, number):
        return {"state": "open", "head": {"sha": self.sha}}
    def get_pull_diff(self, owner, repo, number):
        return "diff --git a/src/example.js b/src/example.js\n--- a/src/example.js\n+++ b/src/example.js\n@@ -1,1 +1,2 @@\n const a = 1;\n+const b = 2;\n"
    def get_pull_reviews(self, owner, repo, number):
        return []
    def create_review(self, owner, repo, number, body, event, comments, commit_id=None):
        self.commit_id = commit_id
        self.posted = comments
        return {"id": 1, "html_url": "https://example.invalid/review"}


def test_dry_run_does_not_publish():
    client = FakeGitHub()
    payload = GitHubPublishInput(owner="o", repo="r", pull_number=1, expected_head_sha=client.sha,
        summary="Review", dry_run=True, comments=[{"path":"src/example.js","line":2,"body":"Check this."}])
    assert publish_review(payload, client)["status"] == "dry_run"
    assert client.posted is None


def test_publish_review_aborts_when_worker_loses_lease():
    client = FakeGitHub()
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[{"path": "src/example.js", "line": 2, "body": "Check this."}],
    )

    def lost_lease():
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        publish_review(payload, client, before_publish=lost_lease)

    assert client.posted is None


def test_publish_review_rechecks_head_sha_before_posting():
    class ChangedHeadGitHub(FakeGitHub):
        requests = 0

        def get_pull_request(self, owner, repo, number):
            self.requests += 1
            sha = self.sha if self.requests == 1 else "new-head"
            return {"state": "open", "head": {"sha": sha}}

    client = ChangedHeadGitHub()
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[{"path": "src/example.js", "line": 2, "body": "Check this."}],
    )

    result = publish_review(payload, client)

    assert result["status"] == "stale_head_sha"
    assert result["current_head_sha"] == "new-head"
    assert client.posted is None


def test_publish_review_posts_validated_comments_with_right_side():
    client = FakeGitHub()
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[{"path": "src/example.js", "line": 2, "body": "Check this."}],
    )

    result = publish_review(payload, client)

    assert result["status"] == "published"
    assert client.posted == [{
        "path": "src/example.js",
        "line": 2,
        "side": "RIGHT",
        "body": "Check this.",
    }]
    assert client.commit_id == client.sha


def test_publish_review_posts_validated_multiline_comment():
    client = FakeGitHub()
    client.get_pull_diff = lambda *args: (
        "diff --git a/src/example.js b/src/example.js\n"
        "--- a/src/example.js\n+++ b/src/example.js\n"
        "@@ -1,1 +1,3 @@\n"
        " const a = 1;\n+const b = 2;\n+const c = 3;\n"
    )
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[{
            "path": "src/example.js",
            "start_line": 2,
            "line": 3,
            "body": "Check this range.",
        }],
    )

    result = publish_review(payload, client)

    assert result["status"] == "published"
    assert client.posted[0]["start_line"] == 2
    assert client.posted[0]["start_side"] == "RIGHT"


def test_publish_review_rejects_invalid_multiline_range():
    client = FakeGitHub()
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[{
            "path": "src/example.js",
            "start_line": 2,
            "line": 2,
            "body": "Invalid range.",
        }],
    )

    result = publish_review(payload, client)

    assert result["status"] == "rejected_invalid_locations"
    assert result["rejected_comments"][0]["reason"] == "start_line must be less than line."


def test_publish_review_rejects_invalid_locations_without_posting():
    client = FakeGitHub()
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[{"path": "src/example.js", "line": 99, "body": "Check this."}],
    )

    result = publish_review(payload, client)

    assert result["status"] == "rejected_invalid_locations"
    assert client.posted is None


def test_publish_review_rejects_outdated_head_without_posting():
    client = FakeGitHub(sha="current1234567")
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha="old1234567",
        summary="Review",
        dry_run=False,
        comments=[{"path": "src/example.js", "line": 2, "body": "Check this."}],
    )

    result = publish_review(payload, client)

    assert result["status"] == "stale_head_sha"
    assert client.posted is None


def test_publish_review_reports_missing_permission():
    class ForbiddenClient(FakeGitHub):
        def create_review(self, *args, commit_id=None):
            raise GitHubAPIError(403, "GitHub token lacks permission to publish pull request reviews.")

    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha="abcdef1234567",
        summary="Review",
        dry_run=False,
        comments=[{"path": "src/example.js", "line": 2, "body": "Check this."}],
    )

    with pytest.raises(GitHubAPIError, match="lacks permission"):
        publish_review(payload, ForbiddenClient())


def test_publish_review_prevents_duplicate_review():
    class DuplicateGitHub(FakeGitHub):
        def get_pull_reviews(self, owner, repo, number):
            return [{"id": 123, "commit_id": self.sha, "body": self.marker}]

        def create_review(self, *args, **kwargs):
            raise AssertionError("Duplicate review should not be published.")

    client = DuplicateGitHub()
    payload = GitHubPublishInput(
        owner="o",
        repo="r",
        pull_number=1,
        expected_head_sha=client.sha,
        summary="Review",
        dry_run=False,
        comments=[
            {
                "path": "src/example.js",
                "line": 2,
                "body": "Check this.",
            }
        ],
    )
    client.marker = _review_idempotency_marker(
        payload,
        [{
            "path": "src/example.js",
            "line": 2,
            "side": "RIGHT",
            "body": "Check this.",
        }],
    )

    result = publish_review(payload, client)

    assert result["status"] == "duplicate_review"
    assert result["review_id"] == 123


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
    client = FakeFixGitHub(sha="different123")

    result = publish_fix(fix_payload(), client)

    assert result["status"] == "stale_head_sha"
    assert client.commits == []


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

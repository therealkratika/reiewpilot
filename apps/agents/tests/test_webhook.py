import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import main
from app.webhook import WebhookEventStore, initiate_review
from app.schemas import Finding, ReviewAnalysis


client = TestClient(main.app)
SECRET = "test-webhook-secret"


def signed_request(payload, event="pull_request"):
    body = json.dumps(payload).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/v1/github/webhooks",
        content=body,
        headers={
            "x-hub-signature-256": f"sha256={signature}",
            "x-github-event": event,
        },
    )


def pull_request_payload(action="opened", sha="a" * 40):
    return {
        "action": action,
        "repository": {"name": "widgets", "owner": {"login": "acme"}},
        "pull_request": {"number": 7, "head": {"sha": sha}},
    }


def test_valid_signature_initiates_review_without_publishing(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "webhook_events", WebhookEventStore())
    calls = []
    monkeypatch.setattr(main, "initiate_review", lambda event: calls.append(event) or {"status": "review_completed"})

    response = signed_request(pull_request_payload())

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["publishing"] is False
    assert calls[0]["head_sha"] == "a" * 40


def test_missing_signature_is_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    response = client.post(
        "/v1/github/webhooks",
        content=b"{}",
        headers={"x-github-event": "pull_request"},
    )
    assert response.status_code == 401


def test_invalid_signature_is_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    response = client.post(
        "/v1/github/webhooks",
        content=b"{}",
        headers={"x-github-event": "pull_request", "x-hub-signature-256": "sha256=invalid"},
    )
    assert response.status_code == 401


def test_irrelevant_action_is_ignored(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    response = signed_request(pull_request_payload(action="closed", sha="b" * 40))
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


def test_malformed_relevant_payload_is_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    response = signed_request({"action": "opened", "repository": {}})
    assert response.status_code == 400


def test_duplicate_event_is_not_processed_twice(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "webhook_events", WebhookEventStore())
    calls = []
    monkeypatch.setattr(main, "initiate_review", lambda event: calls.append(event) or {"status": "review_completed"})
    payload = pull_request_payload(action="synchronize", sha="c" * 40)

    first = signed_request(payload)
    second = signed_request(payload)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(calls) == 1


def test_same_pull_request_head_is_not_reviewed_for_multiple_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "webhook_events", WebhookEventStore())
    calls = []
    monkeypatch.setattr(main, "initiate_review", lambda event: calls.append(event) or {"status": "review_completed"})

    opened = signed_request(pull_request_payload(action="opened", sha="d" * 40))
    synchronized = signed_request(pull_request_payload(action="synchronize", sha="d" * 40))

    assert opened.json()["status"] == "accepted"
    assert synchronized.json()["status"] == "duplicate"
    assert len(calls) == 1


def test_failed_background_review_can_be_retried(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "webhook_events", WebhookEventStore())
    attempts = []

    def fail_once(event):
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        return {"status": "review_completed"}

    monkeypatch.setattr(main, "initiate_review", fail_once)
    payload = pull_request_payload(action="synchronize", sha="e" * 40)

    first = signed_request(payload)
    second = signed_request(payload)

    assert first.status_code == 202
    assert second.json()["status"] == "accepted"
    assert len(attempts) == 2


def test_initiate_review_publishes_comment_only_review(monkeypatch):
    class FakeClient:
        def get_pull_request(self, owner, repo, number):
            return {
                "title": "Sample",
                "state": "open",
                "draft": False,
                "html_url": "https://example.invalid/pr/1",
                "base": {"sha": "base"},
                "head": {"sha": "a" * 40, "ref": "feature"},
            }

        def get_pull_diff(self, owner, repo, number):
            return (
                "diff --git a/src/a.js b/src/a.js\n"
                "--- a/src/a.js\n+++ b/src/a.js\n"
                "@@ -1,1 +1,2 @@\n const a = 1;\n+const b = 2;\n"
            )

        def get_pull_files(self, owner, repo, number):
            return [{"filename": "src/a.js", "status": "modified", "additions": 1, "deletions": 0}]

        def create_review(self, owner, repo, number, body, event, comments):
            self.review = (body, event, comments)
            return {"id": 4, "html_url": "https://example.invalid/review/4"}

    finding = Finding(
        category="security",
        severity="critical",
        title="Unsafe change",
        description="This change can expose data.",
        evidence="The added line lacks an access check.",
        suggested_fix="Check authorization before returning the value.",
        file_path="src/a.js",
        line_start=2,
        line_end=2,
        confidence=0.95,
    )
    monkeypatch.setattr(
        "app.pipeline.analyze_diff",
        lambda diff: ReviewAnalysis(summary="One issue.", findings=[finding]),
    )
    client = FakeClient()

    result = initiate_review(
        {"owner": "acme", "repo": "widgets", "pull_number": 1, "head_sha": "a" * 40},
        client=client,
    )

    assert result["published"] is True
    assert client.review[1] == "COMMENT"
    assert client.review[2][0]["path"] == "src/a.js"
    assert client.review[2][0]["line"] == 2
    assert "**Critical: Unsafe change**" in client.review[2][0]["body"]
    assert "Suggested fix" in client.review[2][0]["body"]


def test_missing_webhook_secret_is_reported(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    response = client.post("/v1/github/webhooks", content=b"{}")
    assert response.status_code == 503

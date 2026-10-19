import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import main
from app.webhook import (
    RedisWebhookEventStore,
    WebhookEventStore,
    _finding_comment,
    initiate_review,
)
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

        def get_pull_reviews(self, owner, repo, number):
            return []

        def create_review(self, owner, repo, number, body, event, comments, commit_id=None):
            self.review = (body, event, comments, commit_id)
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
    assert client.review[3] == "a" * 40
    assert client.review[2][0]["path"] == "src/a.js"
    assert client.review[2][0]["line"] == 2
    assert "**Critical: Unsafe change**" in client.review[2][0]["body"]
    assert "Suggested fix" in client.review[2][0]["body"]


def test_finding_comment_preserves_multiline_range():
    comment = _finding_comment({
        "severity": "high",
        "title": "Range issue",
        "description": "The change has an issue.",
        "evidence": "The added range is unsafe.",
        "suggested_fix": "Correct the range.",
        "file_path": "src/a.js",
        "line_start": 2,
        "line_end": 4,
    })

    assert comment["line"] == 4
    assert comment["start_line"] == 2


def test_initiate_review_skips_stale_webhook(monkeypatch):
    class FakeClient:
        def get_pull_request(self, owner, repo, number):
            return {"head": {"sha": "b" * 40}}

    def unexpected_pipeline(*args, **kwargs):
        raise AssertionError("Pipeline should not run for stale webhook.")

    monkeypatch.setattr(
        "app.webhook.run_github_review_pipeline",
        unexpected_pipeline,
    )
    result = initiate_review(
        {
            "owner": "acme",
            "repo": "widgets",
            "pull_number": 7,
            "head_sha": "a" * 40,
        },
        client=FakeClient(),
    )

    assert result["status"] == "stale_webhook"
    assert result["published"] is False


def test_initiate_review_skips_empty_review(monkeypatch):
    class FakeClient:
        def get_pull_request(self, owner, repo, number):
            return {"head": {"sha": "a" * 40}}

    pipeline_result = {
        "status": "review_completed",
        "review": {
            "summary": "No issues found.",
            "findings": [],
        },
    }
    monkeypatch.setattr(
        "app.webhook.run_github_review_pipeline",
        lambda *args, **kwargs: pipeline_result,
    )
    result = initiate_review(
        {
            "owner": "acme",
            "repo": "widgets",
            "pull_number": 7,
            "head_sha": "a" * 40,
        },
        client=FakeClient(),
    )

    assert result["status"] == "no_findings"
    assert result["published"] is False
    assert result["publication"]["status"] == "skipped"


def test_missing_webhook_secret_is_reported(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    response = client.post("/v1/github/webhooks", content=b"{}")
    assert response.status_code == 503


def test_redis_webhook_event_store_claim_and_release(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data = {}
            self.calls = []

        def set(self, key, value, nx=False, ex=None):
            self.calls.append(("set", (key, value), {"nx": nx, "ex": ex}))
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def eval(self, script, numkeys, key, token):
            self.calls.append(("eval", (script, numkeys, key, token)))
            if self.data.get(key) == token:
                del self.data[key]
                return 1
            return 0

    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    store = RedisWebhookEventStore("redis://localhost:6379/0", ttl_seconds=60)
    key = ("acme/widgets", 7, "a" * 40)

    assert store.claim(key) is True
    store.release(key)

    assert fake_redis.calls[0][0] == "set"
    assert fake_redis.calls[0][1][0] == "reviewpilot:webhook:acme/widgets:7:" + "a" * 40
    assert fake_redis.calls[0][2] == {"nx": True, "ex": 60}
    assert fake_redis.calls[1][0] == "eval"


def test_redis_event_store_claims_event_once(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def eval(self, script, numkeys, key, token):
            if self.data.get(key) == token:
                del self.data[key]
                return 1
            return 0

    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    store = RedisWebhookEventStore("redis://localhost:6379/0")
    event = ("acme/widgets", 7, "abc123")

    assert store.claim(event) is True
    assert store.claim(event) is False


def test_redis_event_store_releases_event(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def eval(self, script, numkeys, key, token):
            if self.data.get(key) == token:
                del self.data[key]
                return 1
            return 0

    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    store = RedisWebhookEventStore("redis://localhost:6379/0")
    event = ("acme/widgets", 7, "abc123")

    assert store.claim(event) is True
    store.release(event)
    assert store.claim(event) is True


def test_redis_release_does_not_delete_another_workers_lock(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def eval(self, script, numkeys, key, token):
            if self.data.get(key) == token:
                del self.data[key]
                return 1
            return 0

    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    first_store = RedisWebhookEventStore("redis://localhost:6379/0")
    second_store = RedisWebhookEventStore("redis://localhost:6379/0")
    event = ("acme/widgets", 7, "abc123")

    assert first_store.claim(event) is True
    redis_key = first_store._key(event)
    del fake_redis.data[redis_key]
    assert second_store.claim(event) is True

    first_store.release(event)

    assert redis_key in fake_redis.data


def test_redis_refresh_only_renews_owned_lock(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def eval(self, script, numkeys, key, token, ttl):
            if self.data.get(key) == token:
                return 1
            return 0

    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    store = RedisWebhookEventStore("redis://localhost:6379/0")
    event = ("acme/widgets", 7, "abc123")

    assert store.claim(event) is True
    assert store.refresh(event) is True
    fake_redis.data[store._key(event)] = "another-worker-token"
    assert store.refresh(event) is False

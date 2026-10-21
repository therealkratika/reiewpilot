import hashlib
import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from fastapi.testclient import TestClient

from app import main
from app.webhook import (
    RedisWebhookEventStore,
    WebhookEventStore,
    _finding_comment,
    initiate_review,
)
from app.job_queue import QueueUnavailableError
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


def test_valid_signature_enqueues_review_job(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    calls = []
    monkeypatch.setattr(
        main,
        "enqueue_webhook_review",
        lambda event: calls.append(event) or ("webhook-job", True),
    )

    response = signed_request(pull_request_payload())

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["publishing"] is True
    assert response.json()["job_id"] == "webhook-job"
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
    queued = set()

    def enqueue(event):
        key = (event["owner"], event["repo"], event["pull_number"], event["head_sha"])
        if key in queued:
            return "webhook-job", False
        queued.add(key)
        return "webhook-job", True

    monkeypatch.setattr(main, "enqueue_webhook_review", enqueue)
    payload = pull_request_payload(action="synchronize", sha="c" * 40)

    first = signed_request(payload)
    second = signed_request(payload)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"


def test_same_pull_request_head_is_not_reviewed_for_multiple_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    queued = set()

    def enqueue(event):
        key = (event["owner"], event["repo"], event["pull_number"], event["head_sha"])
        if key in queued:
            return "webhook-job", False
        queued.add(key)
        return "webhook-job", True

    monkeypatch.setattr(main, "enqueue_webhook_review", enqueue)

    opened = signed_request(pull_request_payload(action="opened", sha="d" * 40))
    synchronized = signed_request(pull_request_payload(action="synchronize", sha="d" * 40))

    assert opened.json()["status"] == "accepted"
    assert synchronized.json()["status"] == "duplicate"


def test_queue_outage_returns_retryable_status_to_github(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(
        main,
        "enqueue_webhook_review",
        lambda event: (_ for _ in ()).throw(QueueUnavailableError("Redis unavailable")),
    )

    response = signed_request(pull_request_payload(action="synchronize", sha="e" * 40))

    assert response.status_code == 503


def test_concurrent_webhook_deliveries_enqueue_only_once(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    lock = threading.Lock()
    queued = set()
    enqueue_calls = []

    def enqueue(event):
        key = (event["owner"], event["repo"], event["pull_number"], event["head_sha"])
        with lock:
            enqueue_calls.append(key)
            if key in queued:
                return "webhook-job", False
            queued.add(key)
            return "webhook-job", True

    monkeypatch.setattr(main, "enqueue_webhook_review", enqueue)
    payload = pull_request_payload(action="synchronize", sha="f" * 40)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: signed_request(payload), range(2)))

    assert sorted(response.json()["status"] for response in responses) == [
        "accepted",
        "duplicate",
    ]
    assert len(enqueue_calls) == 2


def test_concurrent_distinct_webhook_events_are_both_enqueued(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    queued = set()
    lock = threading.Lock()

    def enqueue(event):
        key = (event["pull_number"], event["head_sha"])
        with lock:
            assert key not in queued
            queued.add(key)
        return f"job-{event['pull_number']}", True

    monkeypatch.setattr(main, "enqueue_webhook_review", enqueue)
    first_payload = pull_request_payload(action="synchronize", sha="1" * 40)
    second_payload = pull_request_payload(action="synchronize", sha="2" * 40)
    second_payload["pull_request"]["number"] = 8

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(signed_request, (first_payload, second_payload))
        )

    assert all(response.status_code == 202 for response in responses)
    assert [response.json()["job_id"] for response in responses] == [
        "job-7",
        "job-8",
    ]
    assert len(queued) == 2


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


class FakeLeaseRedis:
    def __init__(self):
        self.data = {}
        self.clock = 0

    def set(self, key, value, nx=False, ex=None):
        if self.get(key) is not None and nx:
            return None
        self.data[key] = (value, self.clock + ex)
        return True

    def get(self, key):
        item = self.data.get(key)
        if item and item[1] > self.clock:
            return item[0]
        self.data.pop(key, None)
        return None

    def eval(self, script, numkeys, key, token, *args):
        if self.get(key) != token:
            return 0
        if "PEXPIRE" in script:
            ttl_ms = args[0]
            self.data[key] = (token, self.clock + ttl_ms / 1000)
            return 1
        if "DEL" in script:
            del self.data[key]
            return 1
        return 0


def _fake_lease_store(monkeypatch, ttl_seconds=60):
    fake_redis = FakeLeaseRedis()
    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    store = RedisWebhookEventStore(
        "redis://localhost:6379/0",
        ttl_seconds=ttl_seconds,
    )
    return fake_redis, store


def test_redis_lease_claim_is_atomic_and_release_is_owned(monkeypatch):
    _, first = _fake_lease_store(monkeypatch)
    second = RedisWebhookEventStore("redis://localhost:6379/0", ttl_seconds=60)
    event = ("acme/widgets", 7, "abc123")

    token = first.claim(event)

    assert token
    assert second.claim(event) is None
    assert first.owns(event, token)
    assert first.release(event, token)
    assert second.claim(event)


def test_expired_worker_cannot_refresh_or_release_new_lease(monkeypatch):
    fake_redis, first = _fake_lease_store(monkeypatch, ttl_seconds=10)
    second = RedisWebhookEventStore("redis://localhost:6379/0", ttl_seconds=10)
    event = ("acme/widgets", 7, "abc123")
    old_token = first.claim(event)

    fake_redis.clock += 11
    new_token = second.claim(event)

    assert new_token and new_token != old_token
    assert not first.owns(event, old_token)
    assert not first.refresh(event, old_token)
    assert not first.release(event, old_token)
    assert second.owns(event, new_token)
    assert second.refresh(event, new_token)


def test_redis_connection_failure_does_not_grant_a_lease(monkeypatch):
    class UnavailableRedis:
        def set(self, *args, **kwargs):
            raise RedisConnectionError("offline")

    monkeypatch.setattr(
        "app.webhook.redis.Redis.from_url",
        lambda *args, **kwargs: UnavailableRedis(),
    )
    store = RedisWebhookEventStore("redis://localhost:6379/0")

    with pytest.raises(RedisConnectionError):
        store.claim(("acme/widgets", 7, "abc123"))

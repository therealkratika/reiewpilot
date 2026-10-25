import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import main
from app.webhook import WebhookEventStore


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


def test_missing_webhook_secret_is_reported(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    response = client.post("/v1/github/webhooks", content=b"{}")
    assert response.status_code == 503

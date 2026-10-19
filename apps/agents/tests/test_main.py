from fastapi.testclient import TestClient

from app import main
from app.main import app


client = TestClient(app)


def test_webhook_lock_heartbeat_refreshes_owned_lock(monkeypatch):
    class FakeStopEvent:
        def __init__(self):
            self.wait_calls = []
            self.results = [False, True]

        def wait(self, interval):
            self.wait_calls.append(interval)
            return self.results.pop(0)

    refreshed = []

    class FakeStore:
        def refresh(self, key):
            refreshed.append(key)
            return True

    monkeypatch.setattr(main, "webhook_events", FakeStore())
    stop_event = FakeStopEvent()
    key = ("acme/widgets", 7, "abc123")

    main.keep_webhook_lock_alive(key, stop_event)

    assert refreshed == [key]
    assert stop_event.wait_calls == [300, 300]


def test_webhook_lock_heartbeat_is_noop_for_memory_store(monkeypatch):
    class FakeStopEvent:
        def wait(self, interval):
            raise AssertionError("In-memory stores should not start a heartbeat loop.")

    monkeypatch.setattr(main, "webhook_events", object())

    main.keep_webhook_lock_alive(("acme/widgets", 7, "abc123"), FakeStopEvent())


def test_review_pipeline_rejects_invalid_request_body():
    response = client.post(
        "/v1/github/pulls/acme/widgets/1/review-pipeline",
        json={"unexpected": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]


def test_review_pipeline_rejects_non_positive_pull_number():
    response = client.post(
        "/v1/github/pulls/acme/widgets/0/review-pipeline",
        json={"propose_fix": False, "validate_fix": False},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "pull_number must be positive."


def test_review_pipeline_allows_vite_cors_origin():
    response = client.options(
        "/v1/github/pulls/acme/widgets/1/review-pipeline",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

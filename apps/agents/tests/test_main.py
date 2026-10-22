import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import AuthenticatedUser
from app.main import app


client = TestClient(app)
TEST_USER = AuthenticatedUser(1, 123, "test-user", "Test User", "", "user-token", "session", "csrf")


@pytest.fixture(autouse=True)
def authenticated_api_user():
    app.dependency_overrides[main.get_current_user] = lambda: TEST_USER
    yield
    app.dependency_overrides.pop(main.get_current_user, None)


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


def test_job_status_reports_persisted_status_when_queue_is_unavailable(monkeypatch):
    class FakeDatabase:
        def get_job(self, job_id):
            return {"job_id": job_id, "owner": "acme", "repo": "widgets", "status": "retrying"}

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    monkeypatch.setattr(main, "authorize_repository", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "queued_job_status",
        lambda job_id: (_ for _ in ()).throw(main.QueueUnavailableError("offline")),
    )

    response = client.get("/v1/github/jobs/webhook-test")

    assert response.status_code == 200
    assert response.json()["status"] == "retrying"


def test_job_status_maps_rq_failed_status(monkeypatch):
    class FakeDatabase:
        def get_job(self, job_id):
            return {"job_id": job_id, "owner": "acme", "repo": "widgets", "status": "retrying"}

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    monkeypatch.setattr(main, "authorize_repository", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "queued_job_status", lambda job_id: "failed")

    response = client.get("/v1/github/jobs/webhook-test")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_sensitive_api_rejects_unauthenticated_requests():
    app.dependency_overrides.pop(main.get_current_user, None)
    try:
        response = client.get("/v1/reviews/history")
    finally:
        app.dependency_overrides[main.get_current_user] = lambda: TEST_USER
    assert response.status_code == 401

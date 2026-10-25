from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


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

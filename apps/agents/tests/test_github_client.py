from unittest.mock import Mock

import pytest
from app.github_client import GitHubAPIError, GitHubClient


def test_missing_token_is_reported():
    with pytest.raises(GitHubAPIError, match="GITHUB_TOKEN is not configured"):
        GitHubClient(token="")._headers()


@pytest.mark.parametrize("value", ["../other", "owner/repo", r"owner\repo", ""])
def test_invalid_identifier_is_rejected(value):
    with pytest.raises(ValueError):
        GitHubClient._identifier(value, "owner")


def test_get_pull_files_fetches_multiple_pages(monkeypatch):
    client = GitHubClient(token="test-token")
    first_page = Mock()
    first_page.ok = True
    first_page.json.return_value = [{"filename": f"file_{i}.py"} for i in range(100)]
    second_page = Mock()
    second_page.ok = True
    second_page.json.return_value = [{"filename": "file_100.py"}]
    responses = [first_page, second_page]
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs["params"]["page"])
        return responses.pop(0)

    monkeypatch.setattr("app.github_client.requests.get", fake_get)

    files = client.get_pull_files("acme", "widgets", 1)

    assert len(files) == 101
    assert calls == [1, 2]


def test_get_pull_reviews_returns_reviews(monkeypatch):
    client = GitHubClient(token="test-token")
    response = Mock()
    response.ok = True
    response.json.return_value = [{"id": 10, "state": "COMMENTED"}]
    monkeypatch.setattr(
        "app.github_client.requests.get",
        lambda *args, **kwargs: response,
    )

    reviews = client.get_pull_reviews("acme", "widgets", 1)

    assert reviews == [{"id": 10, "state": "COMMENTED"}]


def test_get_pull_reviews_fetches_all_pages(monkeypatch):
    client = GitHubClient(token="test-token")
    first_page = Mock()
    first_page.ok = True
    first_page.json.return_value = [{"id": review_id} for review_id in range(100)]
    second_page = Mock()
    second_page.ok = True
    second_page.json.return_value = [{"id": 100}]
    responses = [first_page, second_page]
    pages = []

    def fake_get(url, **kwargs):
        pages.append(kwargs["params"]["page"])
        return responses.pop(0)

    monkeypatch.setattr("app.github_client.requests.get", fake_get)

    reviews = client.get_pull_reviews("acme", "widgets", 1)

    assert len(reviews) == 101
    assert pages == [1, 2]


def test_get_retries_rate_limited_read(monkeypatch):
    client = GitHubClient(token="test-token")
    rate_limited = Mock()
    rate_limited.status_code = 429
    rate_limited.headers = {"Retry-After": "0"}
    successful = Mock()
    successful.status_code = 200
    successful.ok = True
    successful.json.return_value = {"state": "open"}
    responses = [rate_limited, successful]
    attempts = []

    def fake_get(*args, **kwargs):
        attempts.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr("app.github_client.requests.get", fake_get)
    monkeypatch.setattr("app.github_client.time.sleep", lambda _: None)

    result = client.get_pull_request("acme", "widgets", 1)

    assert result == {"state": "open"}
    assert len(attempts) == 2


@pytest.mark.parametrize("status_code", [403, 404, 422])
def test_pull_lookup_surfaces_github_api_errors(monkeypatch, status_code):
    client = GitHubClient(token="test-token")
    response = Mock()
    response.status_code = status_code
    response.ok = False
    response.headers = {}
    monkeypatch.setattr("app.github_client.requests.get", lambda *args, **kwargs: response)

    with pytest.raises(GitHubAPIError) as raised:
        client.get_pull_request("acme", "widgets", 7)

    assert raised.value.status_code == status_code

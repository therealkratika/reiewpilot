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

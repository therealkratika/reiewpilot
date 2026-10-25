import pytest
from app.github_client import GitHubAPIError, GitHubClient


def test_missing_token_is_reported():
    with pytest.raises(GitHubAPIError, match="GITHUB_TOKEN is not configured"):
        GitHubClient(token="")._headers()


@pytest.mark.parametrize("value", ["../other", "owner/repo", r"owner\repo", ""])
def test_invalid_identifier_is_rejected(value):
    with pytest.raises(ValueError):
        GitHubClient._identifier(value, "owner")

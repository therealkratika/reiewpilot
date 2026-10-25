import base64
import os
from typing import Any
from urllib.parse import quote

import requests


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    def __init__(self, token=None, api_url=None, timeout=15):
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self.api_url = (api_url or os.getenv("GITHUB_API_URL", "https://api.github.com")).rstrip("/")
        self.timeout = timeout

    def _headers(self, accept="application/vnd.github+json"):
        if not self.token:
            raise GitHubAPIError(503, "GITHUB_TOKEN is not configured.")
        return {
            "Accept": accept,
            "Authorization": "Bearer " + self.token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ReviewPilot",
        }

    @staticmethod
    def _identifier(value: str, label: str) -> str:
        if not value or value in (".", "..") or "/" in value or "\\" in value:
            raise ValueError(f"Invalid {label}.")
        return quote(value, safe="-_.")

    def get_pull_request(self, owner, repo, pull_number) -> dict[str, Any]:
        response = requests.get(
            f"{self.api_url}/repos/{self._identifier(owner, 'owner')}/{self._identifier(repo, 'repository')}/pulls/{pull_number}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if not response.ok:
            raise GitHubAPIError(response.status_code, f"GitHub PR lookup failed ({response.status_code}).")
        return response.json()

    def get_pull_diff(self, owner, repo, pull_number):
        response = requests.get(
            f"{self.api_url}/repos/{self._identifier(owner, 'owner')}/{self._identifier(repo, 'repository')}/pulls/{pull_number}",
            headers=self._headers("application/vnd.github.diff"),
            timeout=self.timeout,
        )
        if not response.ok:
            raise GitHubAPIError(response.status_code, f"GitHub diff lookup failed ({response.status_code}).")
        return response.text

    def get_pull_files(self, owner, repo, pull_number):
        response = requests.get(
            f"{self.api_url}/repos/{self._identifier(owner, 'owner')}/{self._identifier(repo, 'repository')}/pulls/{pull_number}/files",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=self.timeout,
        )
        if not response.ok:
            raise GitHubAPIError(response.status_code, f"GitHub changed-file lookup failed ({response.status_code}).")
        data = response.json()
        if not isinstance(data, list):
            raise GitHubAPIError(502, "Unexpected changed-files response.")
        return data

    def get_file_content(self, owner, repo, path, ref):
        safe_owner = self._identifier(owner, "owner")
        safe_repo = self._identifier(repo, "repository")
        self._validate_file_path(path)
        response = requests.get(
            f"{self.api_url}/repos/{safe_owner}/{safe_repo}/contents/{quote(path, safe='/.-_')}",
            headers=self._headers(),
            params={"ref": ref},
            timeout=self.timeout,
        )
        if not response.ok:
            raise GitHubAPIError(response.status_code, f"GitHub file-content lookup failed ({response.status_code}).")
        data = response.json()
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubAPIError(422, "File is not available as a regular base64-encoded file.")
        return base64.b64decode(data["content"]).decode("utf-8")

    def create_review(self, owner, repo, pull_number, body, event, comments):
        response = requests.post(
            f"{self.api_url}/repos/{self._identifier(owner, 'owner')}/{self._identifier(repo, 'repository')}/pulls/{pull_number}/reviews",
            headers=self._headers(),
            json={"body": body, "event": event, "comments": comments},
            timeout=self.timeout,
        )
        if not response.ok:
            raise GitHubAPIError(response.status_code, f"GitHub review publication failed ({response.status_code}).")
        return response.json()

    def create_commit_on_branch(self, owner, repo, branch, expected_head_sha, path, content, message):
        self._validate_file_path(path)
        query = """
        mutation CreateCommit($input: CreateCommitOnBranchInput!) {
          createCommitOnBranch(input: $input) {
            commit { oid url }
          }
        }
        """
        variables = {
            "input": {
                "branch": {"repositoryNameWithOwner": f"{owner}/{repo}", "branchName": branch},
                "expectedHeadOid": expected_head_sha,
                "message": {"headline": message},
                "fileChanges": {
                    "additions": [{
                        "path": path,
                        "contents": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    }]
                },
            }
        }
        response = requests.post(
            f"{self.api_url}/graphql",
            headers=self._headers(),
            json={"query": query, "variables": variables},
            timeout=self.timeout,
        )
        if not response.ok:
            if response.status_code in (401, 403):
                raise GitHubAPIError(response.status_code, "GitHub token lacks permission to create commits.")
            raise GitHubAPIError(response.status_code, f"GitHub commit creation failed ({response.status_code}).")
        data = response.json()
        if data.get("errors"):
            message_text = str(data["errors"][0].get("message", "GitHub commit creation failed."))
            if "expectedHeadOid" in message_text or "head" in message_text.lower():
                raise GitHubAPIError(409, "The PR head changed before the commit was created.")
            if "permission" in message_text.lower() or "forbidden" in message_text.lower():
                raise GitHubAPIError(403, "GitHub token lacks permission to create commits.")
            raise GitHubAPIError(502, "GitHub commit creation failed.")
        commit = (data.get("data") or {}).get("createCommitOnBranch", {}).get("commit")
        if not commit:
            raise GitHubAPIError(502, "GitHub did not return the created commit.")
        return commit

    @staticmethod
    def _validate_file_path(path):
        if not path or path == "." or path.startswith("/") or ".." in path.split("/") or "\\" in path:
            raise ValueError("Unsafe repository file path.")

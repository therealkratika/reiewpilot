from typing import Any
from .diff_parser import added_lines_by_file
from .github_client import GitHubAPIError, GitHubClient
from .schemas import GitHubFixPublishInput, GitHubPublishInput


def pull_request_snapshot(owner: str, repo: str, pull_number: int, client=None) -> dict[str, Any]:
    client = client or GitHubClient()
    pr = client.get_pull_request(owner, repo, pull_number)
    diff = client.get_pull_diff(owner, repo, pull_number)
    files = client.get_pull_files(owner, repo, pull_number)
    return {"repository": f"{owner}/{repo}", "pull_number": pull_number, "title": pr.get("title", ""),
        "state": pr.get("state", ""), "draft": bool(pr.get("draft", False)), "html_url": pr.get("html_url", ""),
        "base_sha": (pr.get("base") or {}).get("sha", ""), "head_sha": (pr.get("head") or {}).get("sha", ""),
        "head_ref": (pr.get("head") or {}).get("ref", ""), "diff": diff,
        "changed_files": [{"filename": i.get("filename"), "status": i.get("status"),
                           "additions": i.get("additions"), "deletions": i.get("deletions")} for i in files]}


def publish_review(payload: GitHubPublishInput, client=None) -> dict[str, Any]:
    client = client or GitHubClient()
    pr = client.get_pull_request(payload.owner, payload.repo, payload.pull_number)
    current_sha = (pr.get("head") or {}).get("sha", "")
    if current_sha != payload.expected_head_sha:
        return {"status": "stale_head_sha", "published": False,
            "message": "The PR changed since review. Fetch it again before publishing.",
            "expected_head_sha": payload.expected_head_sha, "current_head_sha": current_sha}
    diff = client.get_pull_diff(payload.owner, payload.repo, payload.pull_number)
    changed = added_lines_by_file(diff)
    comments, rejected = [], []
    for c in payload.comments:
        path = c.path.removeprefix("a/").removeprefix("b/")
        if c.line not in changed.get(path, set()):
            rejected.append({"path": path, "line": c.line, "reason": "Line is not added in current diff."})
        else:
            comments.append({"path": path, "line": c.line, "body": c.body})
    if rejected:
        return {"status": "rejected_invalid_locations", "published": False,
                "accepted_comment_count": len(comments), "rejected_comments": rejected}
    if payload.dry_run:
        return {"status": "dry_run", "published": False,
            "message": "Locations and SHA validated; no write request was sent.",
            "comment_count": len(comments), "event": payload.event, "head_sha": current_sha}
    result = client.create_review(payload.owner, payload.repo, payload.pull_number,
                                  payload.summary, payload.event, comments)
    return {"status": "published", "published": True, "review_id": result.get("id"),
            "html_url": result.get("html_url"), "comment_count": len(comments), "head_sha": current_sha}


def publish_fix(payload: GitHubFixPublishInput, client=None) -> dict[str, Any]:
    client = client or GitHubClient()
    path = payload.file_path.removeprefix("a/").removeprefix("b/")
    GitHubClient._validate_file_path(path)
    pr = client.get_pull_request(payload.owner, payload.repo, payload.pull_number)
    if pr.get("state") != "open":
        return {"status": "closed_pr", "published": False, "message": "Only open pull requests can receive fix commits."}
    current_sha = (pr.get("head") or {}).get("sha", "")
    branch = (pr.get("head") or {}).get("ref", "")
    if current_sha != payload.expected_head_sha:
        return {"status": "stale_head_sha", "published": False,
                "message": "The PR changed since the fix was proposed. Generate a new proposal.",
                "expected_head_sha": payload.expected_head_sha, "current_head_sha": current_sha}
    files = client.get_pull_files(payload.owner, payload.repo, payload.pull_number)
    if not any(item.get("filename") == path for item in files):
        return {"status": "invalid_file_path", "published": False,
                "message": "The proposed file is not part of the current pull request."}
    if not payload.dry_run and not payload.approved:
        return {"status": "approval_required", "published": False,
                "message": "Explicit approval is required before creating a commit."}
    if payload.dry_run:
        return {"status": "dry_run", "published": False,
                "message": "Head SHA and target file validated; no commit was created.",
                "file_path": path, "head_sha": current_sha}
    try:
        commit = client.create_commit_on_branch(
            payload.owner, payload.repo, branch, payload.expected_head_sha,
            path, payload.updated_code, payload.commit_message,
        )
    except GitHubAPIError:
        raise
    return {"status": "published", "published": True, "file_path": path,
            "head_sha": current_sha, "commit_sha": commit.get("oid"), "commit_url": commit.get("url")}

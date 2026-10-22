import hashlib
import json
from typing import Any, Callable
from .diff_parser import added_lines_by_file
from .github_client import GitHubAPIError, GitHubClient
from .schemas import GitHubPublishInput


def _review_idempotency_marker(payload, comments):
    review_data = {
        "repository": f"{payload.owner}/{payload.repo}",
        "pull_number": payload.pull_number,
        "head_sha": payload.expected_head_sha,
        "summary": payload.summary,
        "event": payload.event,
        "comments": comments,
    }
    serialized = json.dumps(
        review_data,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    return f"<!-- reviewpilot:{digest} -->"


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


def publish_review(
    payload: GitHubPublishInput,
    client=None,
    before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    client = client or GitHubClient()
    pr = client.get_pull_request(payload.owner, payload.repo, payload.pull_number)
    if pr.get("state") != "open":
        return {
            "status": "closed_pr",
            "published": False,
            "message": "Only open pull requests can receive reviews.",
        }
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
            continue
        comment = {
            "path": path,
            "line": c.line,
            "side": "RIGHT",
            "body": c.body,
        }
        if c.start_line is not None:
            if c.start_line >= c.line:
                rejected.append({
                    "path": path,
                    "line": c.line,
                    "reason": "start_line must be less than line.",
                })
                continue
            if c.start_line not in changed.get(path, set()):
                rejected.append({
                    "path": path,
                    "line": c.line,
                    "reason": "Start line is not added in current diff.",
                })
                continue
            comment["start_line"] = c.start_line
            comment["start_side"] = "RIGHT"
        comments.append(comment)
    if rejected:
        return {"status": "rejected_invalid_locations", "published": False,
                "accepted_comment_count": len(comments), "rejected_comments": rejected}
    marker = _review_idempotency_marker(payload, comments)
    if payload.dry_run:
        return {"status": "dry_run", "published": False,
            "message": "Locations and SHA validated; no write request was sent.",
            "comment_count": len(comments), "event": payload.event, "head_sha": current_sha}
    existing_reviews = client.get_pull_reviews(
        payload.owner,
        payload.repo,
        payload.pull_number,
    )
    for review in existing_reviews:
        if (
            review.get("commit_id") == current_sha
            and marker in (review.get("body") or "")
        ):
            return {
                "status": "duplicate_review",
                "published": False,
                "message": "This review has already been published.",
                "review_id": review.get("id"),
                "head_sha": current_sha,
            }
    if before_publish is not None:
        before_publish()
    latest_pr = client.get_pull_request(
        payload.owner,
        payload.repo,
        payload.pull_number,
    )
    latest_sha = (latest_pr.get("head") or {}).get("sha", "")
    if latest_pr.get("state") != "open":
        return {
            "status": "closed_pr",
            "published": False,
            "message": "The pull request was closed before the review could be published.",
        }
    if latest_sha != current_sha:
        return {
            "status": "stale_head_sha",
            "published": False,
            "message": "The PR changed before the review could be published.",
            "expected_head_sha": current_sha,
            "current_head_sha": latest_sha,
        }
    if before_publish is not None:
        before_publish()
    result = client.create_review(
        payload.owner,
        payload.repo,
        payload.pull_number,
        f"{payload.summary}\n\n{marker}",
        payload.event,
        comments,
        commit_id=latest_sha,
    )
    return {"status": "published", "published": True, "review_id": result.get("id"),
            "html_url": result.get("html_url"), "comment_count": len(comments), "head_sha": current_sha}


def publish_fix(
    *,
    owner: str,
    repo: str,
    pull_number: int,
    expected_head_sha: str,
    file_path: str,
    updated_code: str,
    commit_message: str,
    dry_run: bool = True,
    client=None,
    before_publish: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    client = client or GitHubClient()
    path = file_path.removeprefix("a/").removeprefix("b/")
    GitHubClient._validate_file_path(path)
    pr = client.get_pull_request(owner, repo, pull_number)
    if pr.get("state") != "open":
        return {"status": "closed_pr", "published": False, "message": "Only open pull requests can receive fix commits."}
    current_sha = (pr.get("head") or {}).get("sha", "")
    branch = (pr.get("head") or {}).get("ref", "")
    if current_sha != expected_head_sha:
        return {"status": "stale_head_sha", "published": False,
                "message": "The PR changed since the fix was proposed. Generate a new proposal.",
                "expected_head_sha": expected_head_sha, "current_head_sha": current_sha}
    files = client.get_pull_files(owner, repo, pull_number)
    if not any(item.get("filename") == path for item in files):
        return {"status": "invalid_file_path", "published": False,
                "message": "The proposed file is not part of the current pull request."}
    if not dry_run and before_publish is None:
        return {"status": "approval_required", "published": False,
                "message": "Explicit approval is required before creating a commit."}
    if dry_run:
        return {"status": "dry_run", "published": False,
                "message": "Head SHA and target file validated; no commit was created.",
                "file_path": path, "head_sha": current_sha}
    if before_publish is not None:
        before_publish(current_sha)
    try:
        commit = client.create_commit_on_branch(
            owner,
            repo,
            branch,
            expected_head_sha,
            path,
            updated_code,
            commit_message,
        )
    except GitHubAPIError:
        raise
    return {"status": "published", "published": True, "file_path": path,
            "head_sha": current_sha, "commit_sha": commit.get("oid"), "commit_url": commit.get("url")}

import hashlib
import hmac
import json
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable

import redis

from .pipeline import run_github_review_pipeline
from .github_client import GitHubClient
from .github_service import publish_review
from .schemas import GitHubPipelineInput, GitHubPublishInput

RELEVANT_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
MAX_PROCESSED_EVENTS = 10_000
DEFAULT_LEASE_TTL_SECONDS = 180


class WebhookEventStore:
    def __init__(
        self,
        max_events: int = MAX_PROCESSED_EVENTS,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ):
        self._events: OrderedDict[tuple[str, int, str], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_events = max_events
        self._ttl = ttl_seconds

    def claim(self, key: tuple[str, int, str]) -> str | None:
        with self._lock:
            current = self._events.get(key)
            if current and current[1] > time.monotonic():
                return None
            token = uuid.uuid4().hex
            self._events[key] = (token, time.monotonic() + self._ttl)
            self._events.move_to_end(key)
            while len(self._events) > self._max_events:
                self._events.popitem(last=False)
            return token

    def owns(self, key: tuple[str, int, str], token: str) -> bool:
        with self._lock:
            current = self._events.get(key)
            return bool(current and current[0] == token and current[1] > time.monotonic())

    def release(self, key: tuple[str, int, str], token: str) -> bool:
        with self._lock:
            current = self._events.get(key)
            if not current or current[0] != token or current[1] <= time.monotonic():
                return False
            del self._events[key]
            return True


class RedisWebhookEventStore:
    RELEASE_SCRIPT = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("DEL", KEYS[1])
    end
    return 0
    """
    RENEW_SCRIPT = """
    if redis.call("GET", KEYS[1]) == ARGV[1] and redis.call("PTTL", KEYS[1]) > 0 then
        return redis.call("PEXPIRE", KEYS[1], ARGV[2])
    end
    return 0
    """

    def __init__(
        self,
        redis_url: str | None = None,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        *,
        connection: redis.Redis | None = None,
    ):
        if ttl_seconds < 1:
            raise ValueError("Webhook lease TTL must be positive.")
        if connection is None and not redis_url:
            raise ValueError("A Redis URL or connection is required.")
        self._redis = connection or redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        self._ttl = ttl_seconds

    @staticmethod
    def _key(key: tuple[str, int, str]) -> str:
        repository, pull_number, head_sha = key
        digest = hashlib.sha256(
            f"{repository.lower()}:{pull_number}:{head_sha}".encode("utf-8")
        ).hexdigest()
        return f"reviewpilot:webhook:lease:{digest}"

    def claim(self, key: tuple[str, int, str]) -> str | None:
        token = uuid.uuid4().hex
        claimed = self._redis.set(
            self._key(key),
            token,
            nx=True,
            ex=self._ttl,
        )
        return token if claimed else None

    def owns(self, key: tuple[str, int, str], token: str) -> bool:
        return self._redis.get(self._key(key)) == token

    def release(self, key: tuple[str, int, str], token: str) -> bool:
        result = self._redis.eval(
            self.RELEASE_SCRIPT,
            1,
            self._key(key),
            token,
        )
        return bool(result)

    def refresh(self, key: tuple[str, int, str], token: str) -> bool:
        result = self._redis.eval(
            self.RENEW_SCRIPT,
            1,
            self._key(key),
            token,
            self._ttl * 1000,
        )
        return bool(result)


def verify_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)


def parse_pull_request_event(body: bytes) -> dict[str, Any]:
    try:
        event = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Webhook body must be valid JSON.") from exc
    if not isinstance(event, dict):
        raise ValueError("Webhook body must be a JSON object.")

    action = event.get("action")
    pull_request = event.get("pull_request")
    repository = event.get("repository")
    if not isinstance(pull_request, dict):
        pull_request = {}
    if not isinstance(repository, dict):
        repository = {}
    repository_owner = repository.get("owner")
    owner = repository_owner.get("login") if isinstance(repository_owner, dict) else None
    repo = repository.get("name")
    pull_number = pull_request.get("number") or event.get("number")
    pull_head = pull_request.get("head")
    head_sha = pull_head.get("sha") if isinstance(pull_head, dict) else None
    if not isinstance(action, str):
        raise ValueError("Webhook action is missing.")
    if action in RELEVANT_ACTIONS and (
        not isinstance(owner, str)
        or not owner
        or not isinstance(repo, str)
        or not repo
        or not isinstance(pull_number, int)
        or isinstance(pull_number, bool)
        or pull_number < 1
        or not isinstance(head_sha, str)
        or not head_sha
    ):
        raise ValueError("Relevant pull request webhook is missing repository, number, or head SHA.")
    return {
        "action": action,
        "owner": owner,
        "repo": repo,
        "pull_number": pull_number,
        "head_sha": head_sha,
    }


def event_key(event: dict[str, Any]) -> tuple[str, int, str]:
    return (f"{event['owner']}/{event['repo']}", event["pull_number"], event["head_sha"])


def _finding_comment(finding: dict[str, Any]) -> dict[str, Any]:
    severity = str(finding["severity"]).capitalize()
    body = (
        f"**{severity}: {finding['title']}**\n\n"
        f"{finding['description']}\n\n"
        f"**Evidence:** {finding['evidence']}\n\n"
        f"**Suggested fix:** {finding['suggested_fix']}"
    )
    comment = {
        "path": finding["file_path"],
        "line": finding["line_end"],
        "body": body,
    }
    if finding["line_start"] < finding["line_end"]:
        comment["start_line"] = finding["line_start"]
    return comment


class WebhookLeaseLostError(RuntimeError):
    pass


def initiate_review(
    event: dict[str, Any],
    client=None,
    before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    client = client or GitHubClient()
    pr = client.get_pull_request(
        event["owner"],
        event["repo"],
        event["pull_number"],
    )
    current_sha = (pr.get("head") or {}).get("sha", "")
    if current_sha != event["head_sha"]:
        return {
            "status": "stale_webhook",
            "published": False,
            "message": "The PR changed after this webhook event. Skipping outdated review.",
            "event_head_sha": event["head_sha"],
            "current_head_sha": current_sha,
        }
    result = run_github_review_pipeline(
        event["owner"],
        event["repo"],
        event["pull_number"],
        GitHubPipelineInput(propose_fix=False, validate_fix=False),
        client=client,
        auto_select_target=True,
    )
    if result.get("status") != "review_completed":
        return result

    review = result["review"]
    if not review["findings"]:
        return {
            **result,
            "status": "no_findings",
            "publication": {
                "status": "skipped",
                "published": False,
                "message": "No valid findings were detected. No GitHub review was created.",
            },
            "published": False,
        }
    payload = GitHubPublishInput(
        owner=event["owner"],
        repo=event["repo"],
        pull_number=event["pull_number"],
        expected_head_sha=event["head_sha"],
        summary=review["summary"],
        event="COMMENT",
        dry_run=False,
        comments=[_finding_comment(finding) for finding in review["findings"]],
    )
    published = publish_review(
        payload,
        client=client,
        before_publish=before_publish,
    )
    return {**result, "publication": published, "published": published["published"]}

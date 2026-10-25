import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from typing import Any

from .pipeline import run_github_review_pipeline
from .schemas import GitHubPipelineInput

RELEVANT_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
MAX_PROCESSED_EVENTS = 10_000


class WebhookEventStore:
    def __init__(self, max_events: int = MAX_PROCESSED_EVENTS):
        self._events: OrderedDict[tuple[str, int, str, str], None] = OrderedDict()
        self._lock = threading.Lock()
        self._max_events = max_events

    def claim(self, key: tuple[str, int, str, str]) -> bool:
        with self._lock:
            if key in self._events:
                return False
            self._events[key] = None
            self._events.move_to_end(key)
            while len(self._events) > self._max_events:
                self._events.popitem(last=False)
            return True

    def release(self, key: tuple[str, int, str, str]) -> None:
        with self._lock:
            self._events.pop(key, None)


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
    owner = (repository or {}).get("owner", {}).get("login")
    repo = (repository or {}).get("name")
    pull_number = (pull_request or {}).get("number") or event.get("number")
    head_sha = ((pull_request or {}).get("head") or {}).get("sha")
    if not isinstance(action, str):
        raise ValueError("Webhook action is missing.")
    if action in RELEVANT_ACTIONS and (
        not isinstance(owner, str)
        or not isinstance(repo, str)
        or not isinstance(pull_number, int)
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


def event_key(event: dict[str, Any]) -> tuple[str, int, str, str]:
    return (f"{event['owner']}/{event['repo']}", event["pull_number"], event["head_sha"], event["action"])


def initiate_review(event: dict[str, Any]) -> dict[str, Any]:
    return run_github_review_pipeline(
        event["owner"],
        event["repo"],
        event["pull_number"],
        GitHubPipelineInput(propose_fix=True, validate_fix=False),
        auto_select_target=True,
    )

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .database import ReviewDatabase
from .github_client import GitHubAPIError, GitHubClient

logger = logging.getLogger("reviewpilot.auth")
SESSION_COOKIE = "reviewpilot_session"
CSRF_COOKIE = "reviewpilot_csrf"
OAUTH_STATE_COOKIE = "reviewpilot_oauth_state"
SESSION_DURATION = timedelta(hours=7)
OAUTH_STATE_DURATION = timedelta(minutes=10)
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 60


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    github_id: int
    username: str
    display_name: str
    avatar_url: str
    access_token: str
    session_hash: str
    csrf_hash: str

    def public_profile(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "github_id": self.github_id,
            "username": self.username,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
        }


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fernet() -> Fernet:
    key = os.getenv("SESSION_ENCRYPTION_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        logger.error("SESSION_ENCRYPTION_KEY is invalid")
        raise HTTPException(status_code=503, detail="Authentication is not configured.") from exc


def _oauth_config() -> tuple[str, str, str]:
    client_id = os.getenv("GITHUB_APP_CLIENT_ID", "")
    client_secret = os.getenv("GITHUB_APP_CLIENT_SECRET", "")
    callback_url = os.getenv("GITHUB_OAUTH_CALLBACK_URL", "")
    if not client_id or not client_secret or not callback_url:
        raise HTTPException(status_code=503, detail="GitHub App OAuth is not configured.")
    return client_id, client_secret, callback_url


def _web_origin() -> str:
    origin = os.getenv("WEB_ORIGIN", "").rstrip("/")
    if origin:
        return origin
    if os.getenv("APP_ENV", "development").lower() == "production":
        raise HTTPException(status_code=503, detail="WEB_ORIGIN is not configured.")
    return "http://localhost:5173"


def _cookie_settings() -> tuple[bool, str]:
    secure = os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() not in {
        "0", "false", "no",
    }
    same_site = os.getenv("SESSION_COOKIE_SAMESITE", "none" if secure else "lax").lower()
    if same_site not in {"lax", "strict", "none"}:
        raise HTTPException(status_code=503, detail="Session cookie configuration is invalid.")
    if same_site == "none" and not secure:
        raise HTTPException(
            status_code=503,
            detail="SameSite=None session cookies require Secure cookies.",
        )
    return secure, same_site


def start_github_login(database: ReviewDatabase | None = None) -> RedirectResponse:
    client_id, _, callback_url = _oauth_config()
    _web_origin()
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    (database or ReviewDatabase()).create_oauth_state(
        _hash(state),
        now,
        now + OAUTH_STATE_DURATION,
    )
    secure, _ = _cookie_settings()
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
        "allow_signup": "false",
    })
    response = RedirectResponse(
        f"https://github.com/login/oauth/authorize?{query}",
        status_code=302,
    )
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=int(OAUTH_STATE_DURATION.total_seconds()),
        path="/",
    )
    return response


def _exchange_oauth_code(code: str, client_id: str, client_secret: str) -> tuple[str, dict[str, Any]]:
    try:
        response = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json", "User-Agent": "ReviewPilot"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
            },
            timeout=10,
        )
        if not response.ok:
            raise HTTPException(status_code=502, detail="GitHub OAuth exchange failed.")
        try:
            token_data = response.json()
        except ValueError as exc:
            logger.warning("GitHub OAuth exchange returned invalid JSON")
            raise HTTPException(status_code=502, detail="GitHub OAuth exchange failed.") from exc
        if not isinstance(token_data, dict):
            raise HTTPException(status_code=502, detail="GitHub OAuth exchange returned an invalid response.")
        token = token_data.get("access_token")
        if not isinstance(token, str) or not token:
            logger.warning("GitHub OAuth exchange returned no access token")
            raise HTTPException(status_code=401, detail="GitHub authorization was not completed.")
        profile_response = requests.get(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ReviewPilot",
            },
            timeout=10,
        )
        if not profile_response.ok:
            raise HTTPException(status_code=502, detail="Could not verify the GitHub account.")
        try:
            profile = profile_response.json()
        except ValueError as exc:
            logger.warning("GitHub profile endpoint returned invalid JSON")
            raise HTTPException(status_code=502, detail="Could not verify the GitHub account.") from exc
        if not isinstance(profile, dict):
            raise HTTPException(status_code=502, detail="GitHub returned an invalid account profile.")
        if not isinstance(profile.get("id"), int) or not isinstance(profile.get("login"), str):
            raise HTTPException(status_code=502, detail="GitHub returned an invalid account profile.")
        return token, profile
    except requests.RequestException as exc:
        logger.warning("GitHub OAuth provider request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="GitHub OAuth is temporarily unavailable.") from exc


def complete_github_login(
    request: Request,
    code: str | None,
    state: str | None,
    database: ReviewDatabase | None = None,
) -> RedirectResponse:
    db = database or ReviewDatabase()
    state_cookie = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not state or not state_cookie or not hmac.compare_digest(state, state_cookie):
        raise HTTPException(status_code=400, detail="OAuth state validation failed.")
    now = datetime.now(timezone.utc)
    if not db.consume_oauth_state(_hash(state), now):
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired.")
    if not code:
        raise HTTPException(status_code=400, detail="GitHub did not return an authorization code.")

    client_id, client_secret, _ = _oauth_config()
    token, profile = _exchange_oauth_code(code, client_id, client_secret)
    user_id = db.upsert_github_user(profile, now)
    session_secret = secrets.token_urlsafe(32)
    csrf_secret = secrets.token_urlsafe(32)
    expires_at = now + SESSION_DURATION
    encrypted_token = _fernet().encrypt(token.encode("utf-8")).decode("ascii")
    session_hash = _hash(session_secret)
    db.create_user_session(
        user_id,
        session_hash,
        _hash(csrf_secret),
        encrypted_token,
        now,
        expires_at,
    )

    secure, same_site = _cookie_settings()
    web_origin = _web_origin()
    response = RedirectResponse(f"{web_origin}/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session_secret,
        httponly=True,
        secure=secure,
        samesite=same_site,
        max_age=int(SESSION_DURATION.total_seconds()),
        expires=expires_at,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_secret,
        httponly=False,
        secure=secure,
        samesite=same_site,
        max_age=int(SESSION_DURATION.total_seconds()),
        expires=expires_at,
        path="/",
    )
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    logger.info("GitHub login completed for user_id=%s", user_id)
    return response


def _load_user(request: Request, database: ReviewDatabase | None = None) -> AuthenticatedUser:
    remote_host = request.client.host if request.client else "unknown"
    _rate_limit(request, f"ip:{remote_host}")
    session_secret = request.cookies.get(SESSION_COOKIE, "")
    if not session_secret:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session = (database or ReviewDatabase()).get_user_session(
        _hash(session_secret),
        datetime.now(timezone.utc),
    )
    if session is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.")
    csrf_secret = request.cookies.get(CSRF_COOKIE, "")
    if not csrf_secret or not hmac.compare_digest(_hash(csrf_secret), session["csrf_hash"]):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        header_token = request.headers.get("x-csrf-token", "")
        if not header_token or not hmac.compare_digest(header_token, csrf_secret):
            raise HTTPException(status_code=403, detail="CSRF validation failed.")
        origin = request.headers.get("origin")
        allowed_origin = _web_origin()
        if origin and origin.rstrip("/") != allowed_origin:
            raise HTTPException(status_code=403, detail="Request origin is not allowed.")
    try:
        access_token = _fernet().decrypt(
            session["encrypted_access_token"].encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        logger.error("Could not decrypt stored GitHub token for user_id=%s", session["user_id"])
        raise HTTPException(status_code=503, detail="Authentication session could not be loaded.") from exc
    _rate_limit(request, f"user:{session['user_id']}")
    return AuthenticatedUser(
        id=session["user_id"],
        github_id=session["github_id"],
        username=session["github_username"],
        display_name=session["display_name"],
        avatar_url=session["avatar_url"],
        access_token=access_token,
        session_hash=session["session_hash"],
        csrf_hash=session["csrf_hash"],
    )


def get_current_user(request: Request) -> AuthenticatedUser:
    return _load_user(request)


def authorize_repository(
    user: AuthenticatedUser,
    owner: str,
    repo: str,
    *,
    write: bool = False,
) -> GitHubClient:
    client = GitHubClient(token=user.access_token)
    try:
        permission = client.get_repository_permission(owner, repo)
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Repository was not found.") from exc
        if exc.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="GitHub authorization expired. Sign in again.",
            ) from exc
        if exc.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="You are not authorized to access this repository.",
            ) from exc
        if exc.status_code == 429:
            raise HTTPException(status_code=429, detail="GitHub rate limit exceeded.") from exc
        logger.warning(
            "Repository authorization provider failure user_id=%s status=%s",
            user.id,
            exc.status_code,
        )
        raise HTTPException(status_code=502, detail="Repository authorization could not be verified.") from exc
    expected_name = f"{owner}/{repo}".lower()
    if str(permission.get("full_name", "")).lower() != expected_name:
        raise HTTPException(status_code=404, detail="Repository was not found.")
    if write:
        permitted = permission["push"] or permission["admin"] or permission["maintain"]
    else:
        permitted = (
            permission["pull"]
            or permission["push"]
            or permission["admin"]
            or permission["maintain"]
        )
    if not permitted:
        raise HTTPException(status_code=403, detail="You are not authorized to access this repository.")
    try:
        ReviewDatabase().record_repository_access(
            user.id,
            owner,
            repo,
            "write" if permission["push"] or permission["admin"] or permission["maintain"] else "read",
            datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("Could not record repository access user_id=%s", user.id)
        raise HTTPException(status_code=503, detail="Repository authorization could not be recorded.")
    return client


_rate_lock = threading.Lock()
_local_requests: dict[str, deque[float]] = defaultdict(deque)
_redis_client = None


def _rate_limit(request: Request, key: str) -> None:
    global _redis_client
    try:
        limit = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", RATE_LIMIT_MAX_REQUESTS))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Rate limit configuration is invalid.") from exc
    if limit < 1:
        raise HTTPException(status_code=503, detail="Rate limit configuration is invalid.")
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            import redis

            if _redis_client is None:
                _redis_client = redis.Redis.from_url(redis_url, socket_timeout=1)
            rate_key = f"reviewpilot:ratelimit:{_hash(key)}:{int(time.time() // RATE_LIMIT_WINDOW_SECONDS)}"
            count = _redis_client.eval(
                "local count = redis.call('INCR', KEYS[1]); "
                "if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]); end; "
                "return count",
                1,
                rate_key,
                RATE_LIMIT_WINDOW_SECONDS + 1,
            )
        except Exception as exc:
            logger.warning("Redis rate limiting unavailable: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Rate limiting is unavailable.") from exc
    else:
        rate_key = f"{key}:{request.client.host if request.client else 'unknown'}"
        now = time.monotonic()
        with _rate_lock:
            timestamps = _local_requests[rate_key]
            while timestamps and timestamps[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
                timestamps.popleft()
            timestamps.append(now)
            count = len(timestamps)
            if len(_local_requests) > 10_000:
                expired_keys = [
                    stored_key
                    for stored_key, values in _local_requests.items()
                    if not values or values[-1] <= now - RATE_LIMIT_WINDOW_SECONDS
                ]
                for expired_key in expired_keys[:1000]:
                    _local_requests.pop(expired_key, None)
    if count > limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again shortly.")


def enforce_anonymous_rate_limit(request: Request, purpose: str) -> None:
    host = request.client.host if request.client else "unknown"
    _rate_limit(request, f"{purpose}:{host}")

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from app import auth, main
from app.auth import AuthenticatedUser
from app.database import ReviewDatabase
from app.schemas import GitHubFixPublishInput, GitHubPublishInput


def _request(method="GET", cookies=None, headers=None):
    headers = dict(headers or {})
    if cookies:
        headers["cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    encoded_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({
        "type": "http",
        "method": method,
        "path": "/",
        "headers": encoded_headers,
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "scheme": "http",
    })


def _database(tmp_path, monkeypatch):
    database = ReviewDatabase(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(auth, "_rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "lax")
    return database


def _create_session(database, *, expires_at=None, csrf="csrf-secret"):
    now = datetime.now(timezone.utc)
    user_id = database.upsert_github_user(
        {"id": 101, "login": "test-user", "name": "Test User", "avatar_url": ""},
        now,
    )
    session_secret = "opaque-session-secret"
    database.create_user_session(
        user_id,
        auth._hash(session_secret),
        auth._hash(csrf),
        auth._fernet().encrypt(b"github-user-token").decode("ascii"),
        now,
        expires_at or now + timedelta(hours=1),
    )
    return user_id, session_secret, csrf


def test_invalid_and_expired_sessions_are_rejected(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as missing:
        auth._load_user(_request(), database)
    assert missing.value.status_code == 401

    _, session_secret, csrf = _create_session(
        database,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        csrf="expired-csrf",
    )
    with pytest.raises(HTTPException) as expired:
        auth._load_user(
            _request(cookies={
                auth.SESSION_COOKIE: session_secret,
                auth.CSRF_COOKIE: "expired-csrf",
            }),
            database,
        )
    assert expired.value.status_code == 401


def test_valid_session_decrypts_token_and_requires_csrf_for_writes(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch)
    user_id, session_secret, csrf = _create_session(database)
    request = _request(cookies={
        auth.SESSION_COOKIE: session_secret,
        auth.CSRF_COOKIE: csrf,
    })
    user = auth._load_user(request, database)
    assert user.id == user_id
    assert user.access_token == "github-user-token"

    with pytest.raises(HTTPException) as invalid_csrf:
        auth._load_user(
            _request(
                method="POST",
                cookies={auth.SESSION_COOKIE: session_secret, auth.CSRF_COOKIE: csrf},
                headers={"x-csrf-token": "forged"},
            ),
            database,
        )
    assert invalid_csrf.value.status_code == 403

    valid_write = auth._load_user(
        _request(
            method="POST",
            cookies={auth.SESSION_COOKIE: session_secret, auth.CSRF_COOKIE: csrf},
            headers={"x-csrf-token": csrf, "origin": "http://localhost:5173"},
        ),
        database,
    )
    assert valid_write.username == "test-user"


def test_rate_limit_blocks_requests_after_configured_window_limit(monkeypatch):
    auth._local_requests.clear()
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "1")
    request = _request()
    auth._rate_limit(request, "test-user")
    with pytest.raises(HTTPException) as limited:
        auth._rate_limit(request, "test-user")
    assert limited.value.status_code == 429


def test_production_requires_an_explicit_web_origin(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("WEB_ORIGIN", raising=False)
    with pytest.raises(HTTPException) as missing_origin:
        auth._web_origin()
    assert missing_origin.value.status_code == 503


def test_oauth_state_is_one_time_and_tokens_are_only_in_encrypted_storage(
    tmp_path,
    monkeypatch,
):
    database = _database(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://localhost:8000/v1/auth/callback")
    monkeypatch.setattr(
        auth,
        "_exchange_oauth_code",
        lambda *_args: (
            "github-user-token",
            {"id": 202, "login": "oauth-user", "name": "OAuth User", "avatar_url": ""},
        ),
    )

    login_response = auth.start_github_login(database)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
    state_cookie = login_response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    callback_request = _request(cookies={auth.OAUTH_STATE_COOKIE: state_cookie})
    callback = auth.complete_github_login(callback_request, "oauth-code", state, database)

    cookies = callback.headers.getlist("set-cookie")
    session_cookie = next(value for value in cookies if auth.SESSION_COOKIE in value)
    csrf_cookie = next(value for value in cookies if auth.CSRF_COOKIE in value)
    assert "httponly" in session_cookie.lower()
    assert "httponly" not in csrf_cookie.lower()
    assert "github-user-token" not in session_cookie

    with pytest.raises(HTTPException) as replay:
        auth.complete_github_login(callback_request, "oauth-code", state, database)
    assert replay.value.status_code == 400


def test_repository_permission_is_checked_with_user_credential(monkeypatch):
    user = AuthenticatedUser(1, 101, "member", "Member", "", "user-token", "session", "csrf")

    class FakeGitHubClient:
        def __init__(self, token):
            assert token == "user-token"

        def get_repository_permission(self, owner, repo):
            assert (owner, repo) == ("acme", "private")
            return {"full_name": "acme/private", "pull": False, "push": False,
                    "admin": False, "maintain": False}

    monkeypatch.setattr(auth, "GitHubClient", FakeGitHubClient)
    with pytest.raises(HTTPException) as denied:
        auth.authorize_repository(user, "acme", "private")
    assert denied.value.status_code == 403


def test_review_publication_requires_repository_write_permission(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")

    def deny_write(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No write permission.")

    monkeypatch.setattr(main, "authorize_repository", deny_write)
    monkeypatch.setattr(
        main,
        "publish_review",
        lambda *_args, **_kwargs: pytest.fail("Review publication must not be called."),
    )
    payload = GitHubPublishInput(
        owner="acme",
        repo="private",
        pull_number=1,
        expected_head_sha="a" * 40,
        summary="Review",
        dry_run=False,
    )
    with pytest.raises(HTTPException) as denied:
        main.publish_github_review(payload, user)
    assert denied.value.status_code == 403


def test_job_status_is_hidden_without_repository_authorization(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")

    class FakeDatabase:
        def get_job(self, _job_id):
            return {"owner": "acme", "repo": "private"}

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    monkeypatch.setattr(
        main,
        "authorize_repository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=404, detail="Repository was not found.")
        ),
    )
    with pytest.raises(HTTPException) as denied:
        main.get_webhook_job("another-users-job", user)
    assert denied.value.status_code == 404


def test_fix_approval_cannot_be_forged_or_used_by_another_user(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")

    class FakeDatabase:
        def get_fix_approval(self, approval_id, user_id):
            assert (approval_id, user_id) == (11, 7)
            return None

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    with pytest.raises(HTTPException) as denied:
        main.publish_github_fix(GitHubFixPublishInput(approval_id=11), user)
    assert denied.value.status_code == 404


def test_stale_sha_approval_is_rejected(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")
    now = datetime.now(timezone.utc)
    approval_record = {
        "consumed_at": None,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "head_sha": "old-sha",
        "proposal_head_sha": "new-sha",
    }

    class FakeDatabase:
        def get_fix_approval(self, _approval_id, _user_id):
            return approval_record

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    with pytest.raises(HTTPException) as stale:
        main.publish_github_fix(GitHubFixPublishInput(approval_id=11), user)
    assert stale.value.status_code == 409


def test_current_pr_head_change_invalidates_an_approval(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")
    now = datetime.now(timezone.utc)
    record = {
        "consumed_at": None,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "head_sha": "a" * 40,
        "proposal_head_sha": "a" * 40,
        "owner": "acme",
        "repo": "widgets",
        "pull_number": 2,
        "file_path": "src/index.js",
        "updated_code": "updated",
    }

    class FakeDatabase:
        def get_fix_approval(self, _approval_id, _user_id):
            return record

    class ChangedHeadGitHub:
        def get_pull_request(self, *_args):
            return {"state": "open", "head": {"sha": "b" * 40}}

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    monkeypatch.setattr(main, "authorize_repository", lambda *_args, **_kwargs: ChangedHeadGitHub())
    with pytest.raises(HTTPException) as stale:
        main.publish_github_fix(
            GitHubFixPublishInput(approval_id=11, dry_run=False),
            user,
        )
    assert stale.value.status_code == 409


def test_authenticated_user_can_create_current_head_approval(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")
    proposal = {
        "owner": "acme",
        "repo": "widgets",
        "pull_number": 2,
        "head_sha": "a" * 40,
        "repository_id": 99,
    }
    persisted = {}
    audit_events = []

    class FakeDatabase:
        def get_fix_proposal(self, proposal_id):
            assert proposal_id == 22
            return proposal

        def record_audit_event(self, **_values):
            audit_events.append(_values)

        def create_fix_approval(self, **values):
            persisted.update(values)
            return 31

    class FakeGitHub:
        def get_pull_request(self, *_args):
            return {"state": "open", "head": {"sha": "a" * 40}}

    monkeypatch.setattr(main, "ReviewDatabase", FakeDatabase)
    monkeypatch.setattr(main, "authorize_repository", lambda *_args, **_kwargs: FakeGitHub())
    result = main.approve_fix_proposal(22, user)
    assert result["approval_id"] == 31
    assert persisted["user_id"] == user.id
    assert persisted["repository_id"] == 99
    assert persisted["head_sha"] == "a" * 40
    assert audit_events[0]["action"] == "fix.approve"


def test_valid_server_approval_is_rechecked_and_consumed_before_commit(monkeypatch):
    user = AuthenticatedUser(7, 707, "user", "User", "", "token", "session", "csrf")
    now = datetime.now(timezone.utc)
    record = {
        "consumed_at": None,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "head_sha": "a" * 40,
        "proposal_head_sha": "a" * 40,
        "owner": "acme",
        "repo": "widgets",
        "pull_number": 2,
        "file_path": "src/index.js",
        "updated_code": "updated",
    }

    class FakeDatabase:
        consumed = False
        audit_events = []

        def get_fix_approval(self, _approval_id, _user_id):
            return record

        def consume_fix_approval(self, *_args):
            self.consumed = True
            return True

        def record_audit_event(self, **values):
            self.audit_events.append(values)

        def update_fix_publication(self, *_args):
            return None

    class FakeGitHub:
        commits = []

        def get_pull_request(self, *_args):
            return {"state": "open", "head": {"sha": "a" * 40, "ref": "feature"}}

        def get_pull_files(self, *_args):
            return [{"filename": "src/index.js"}]

        def create_commit_on_branch(self, *args):
            self.commits.append(args)
            return {"oid": "b" * 40, "url": "https://github.com/acme/widgets/commit"}

    database = FakeDatabase()
    github = FakeGitHub()
    monkeypatch.setattr(main, "ReviewDatabase", lambda: database)
    monkeypatch.setattr(
        main,
        "authorize_repository",
        lambda *_args, **_kwargs: github,
    )

    result = main.publish_github_fix(
        GitHubFixPublishInput(approval_id=11, dry_run=False),
        user,
    )
    assert result.status_code == 200
    assert b'"status":"published"' in result.body
    assert database.consumed is True
    assert database.audit_events[0]["action"] == "fix.publish"
    assert len(github.commits) == 1

from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.database import AGENT_ROOT, ReviewDatabase
from app.models import (
    AuditLog,
    FixApproval,
    FixProposal,
    Repository,
    ReviewFinding,
    ReviewHistory,
    User,
    WebhookJob,
)


def _event():
    return {
        "owner": "acme",
        "repo": "widgets",
        "pull_number": 7,
        "head_sha": "a" * 40,
        "action": "synchronize",
    }


def _result():
    return {
        "status": "review_completed",
        "pull_request": {
            "repository": "acme/widgets",
            "pull_number": 7,
            "title": "Improve widgets",
            "state": "open",
            "head_sha": "a" * 40,
            "html_url": "https://github.com/acme/widgets/pull/7",
        },
        "review": {
            "summary": "One issue found.",
            "findings": [
                {
                    "category": "bug",
                    "severity": "high",
                    "title": "Missing guard",
                    "description": "A concrete input can cause a runtime failure.",
                    "evidence": "The added code accesses a missing value.",
                    "suggested_fix": "Check the value before accessing it.",
                    "file_path": "src/index.js",
                    "line_start": 3,
                    "line_end": 3,
                    "confidence": 0.9,
                }
            ],
        },
        "fix": {
            "status": "proposal_ready",
            "file_path": "src/index.js",
            "explanation": "Adds the missing guard.",
            "updated_code": "const value = safe();",
            "patch": "-const value = unsafe();\n+const value = safe();",
            "tests_to_add": ["Add a missing-value regression test."],
            "assumptions": ["safe() returns null when unavailable."],
            "structural_validation": {"passed": True},
        },
    }


def test_migrations_and_persistent_review_records(tmp_path):
    database = ReviewDatabase(tmp_path / "reviewpilot.sqlite3")
    event = _event()
    job_id = "webhook-" + "b" * 64

    database.register_webhook_job(job_id, event)
    database.register_webhook_job(job_id, event)
    database.update_job(job_id, "started", increment_attempts=True)
    review_id = database.save_review_result(event, _result(), job_id=job_id)
    database.update_review_publication(
        "acme",
        "widgets",
        7,
        event["head_sha"],
        {"status": "published", "review_id": "42", "html_url": "https://example.invalid/review"},
    )
    database.update_fix_publication(
        "acme",
        "widgets",
        7,
        event["head_sha"],
        "src/index.js",
        {"status": "published", "commit_sha": "c" * 40, "commit_url": "https://example.invalid/commit"},
    )
    database.update_job(job_id, "completed")

    assert review_id is not None
    assert database.get_job(job_id)["status"] == "completed"
    assert database.get_job(job_id)["attempts"] == 1

    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(Repository)) == 1
        assert session.scalar(select(func.count()).select_from(WebhookJob)) == 1
        finding = session.scalar(select(ReviewFinding))
        review = session.get(ReviewHistory, review_id)
        proposal = session.scalar(select(FixProposal))

    assert finding.title == "Missing guard"
    assert review.publication_status == "published"
    assert review.publication_id == "42"
    assert proposal.publication_status == "published"
    assert proposal.commit_sha == "c" * 40
    assert proposal.updated_code == "const value = safe();"


def test_manual_review_history_can_be_saved_without_webhook_job(tmp_path):
    database = ReviewDatabase(tmp_path / "reviewpilot.sqlite3")

    review_id = database.save_review_result(_event(), _result())

    assert review_id is not None
    with Session(database.engine) as session:
        history = session.get(ReviewHistory, review_id)
    assert history.job_id is None


def test_review_history_and_fix_approvals_are_scoped_to_user(tmp_path):
    database = ReviewDatabase(tmp_path / "scoped.sqlite3")
    now = datetime.now(timezone.utc)
    user_one = database.upsert_github_user(
        {"id": 101, "login": "one", "name": "One", "avatar_url": ""},
        now,
    )
    user_two = database.upsert_github_user(
        {"id": 202, "login": "two", "name": "Two", "avatar_url": ""},
        now,
    )
    review_id = database.save_review_result(_event(), _result(), user_id=user_one)
    proposal_id = database.get_fix_proposal_id(review_id)
    proposal = database.get_fix_proposal(proposal_id)
    approval_id = database.create_fix_approval(
        fix_proposal_id=proposal_id,
        user_id=user_one,
        repository_id=proposal["repository_id"],
        pull_number=proposal["pull_number"],
        head_sha=proposal["head_sha"],
        approved_at=now,
        expires_at=now + timedelta(minutes=15),
    )

    assert len(database.list_user_review_history(user_one)) == 1
    assert database.list_user_review_history(user_two) == []
    assert database.get_fix_approval(approval_id, user_one)["head_sha"] == _event()["head_sha"]
    assert database.get_fix_approval(approval_id, user_two) is None

    with Session(database.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 2
        assert session.scalar(select(func.count()).select_from(FixApproval)) == 1


def test_logout_revokes_session_and_persists_audit_record(tmp_path):
    database = ReviewDatabase(tmp_path / "logout.sqlite3")
    now = datetime.now(timezone.utc)
    user_id = database.upsert_github_user(
        {"id": 303, "login": "logout-user", "name": "Logout User", "avatar_url": ""},
        now,
    )
    session_hash = "f" * 64
    database.create_user_session(
        user_id,
        session_hash,
        "c" * 64,
        "encrypted",
        now,
        now + timedelta(hours=1),
    )
    assert database.revoke_user_session(session_hash, now) is True
    assert database.get_user_session(session_hash, now) is None

    with Session(database.engine) as session:
        audit = session.scalar(select(AuditLog))
    assert audit.user_id == user_id
    assert audit.action == "auth.logout"
    assert audit.outcome == "revoked"


def test_auth_migration_preserves_existing_review_rows(tmp_path):
    database_path = tmp_path / "existing.sqlite3"
    url = f"sqlite:///{database_path}"
    config = Config(str(AGENT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0001_initial")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO repositories "
                "(id, owner, name, created_at, updated_at) VALUES "
                "(1, 'acme', 'widgets', '2025-01-01', '2025-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO pull_requests "
                "(id, repository_id, number, title, state, head_sha, html_url, created_at, updated_at) "
                "VALUES (1, 1, 7, 'Existing PR', 'open', :sha, '', '2025-01-01', '2025-01-01')"
            ),
            {"sha": "a" * 40},
        )
        connection.execute(
            text(
                "INSERT INTO review_history "
                "(id, job_id, pull_request_id, head_sha, summary, status, publication_status, "
                "created_at, updated_at) VALUES "
                "(1, NULL, 1, :sha, 'Existing review', 'review_completed', "
                "'not_attempted', '2025-01-01', '2025-01-01')"
            ),
            {"sha": "a" * 40},
        )
    command.upgrade(config, "head")

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, summary, user_id FROM review_history WHERE id=1")
        ).one()
    assert row == (1, "Existing review", None)

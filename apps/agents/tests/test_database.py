from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import ReviewDatabase
from app.models import FixProposal, Repository, ReviewFinding, ReviewHistory, WebhookJob


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

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event as sqlalchemy_event, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    FixApproval,
    FixProposal,
    OAuthState,
    PullRequest,
    Repository,
    RepositoryAccess,
    ReviewFinding,
    ReviewHistory,
    User,
    UserSession,
    WebhookJob,
)

AGENT_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_LOCK = threading.Lock()
_MIGRATED_URLS: set[str] = set()


def database_url(path: str | Path | None = None) -> str:
    if path is not None:
        return f"sqlite:///{Path(path).resolve()}"
    configured = os.getenv("DATABASE_URL")
    if configured:
        if configured.startswith("postgres://"):
            return configured.replace("postgres://", "postgresql+psycopg://", 1)
        if configured.startswith("postgresql://"):
            return configured.replace("postgresql://", "postgresql+psycopg://", 1)
        return configured
    local_path = Path(os.getenv("DATABASE_PATH", "data/reviewpilot.sqlite3")).resolve()
    return f"sqlite:///{local_path}"


class ReviewDatabase:
    def __init__(self, path: str | Path | None = None):
        self.url = database_url(path)
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite:") else {}
        if self.url.startswith("sqlite:") and not self.url.endswith(":memory:"):
            sqlite_path = Path(self.url.removeprefix("sqlite:///"))
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            self.url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if self.url.startswith("sqlite:"):
            sqlalchemy_event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)

    @staticmethod
    def _enable_sqlite_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def initialize(self) -> None:
        with _MIGRATION_LOCK:
            if self.url in _MIGRATED_URLS:
                return
            config = Config(str(AGENT_ROOT / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
            command.upgrade(config, "head")
            _MIGRATED_URLS.add(self.url)

    def create_oauth_state(self, state_hash: str, now: datetime, expires_at: datetime) -> None:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            session.execute(delete(OAuthState).where(OAuthState.expires_at <= now))
            session.add(OAuthState(state_hash=state_hash, created_at=now, expires_at=expires_at))

    def consume_oauth_state(self, state_hash: str, now: datetime) -> bool:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                delete(OAuthState).where(
                    OAuthState.state_hash == state_hash,
                    OAuthState.expires_at > now,
                )
            )
            return result.rowcount == 1

    def upsert_github_user(self, profile: dict[str, Any], now: datetime) -> int:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            user = session.scalar(select(User).where(User.github_id == int(profile["id"])))
            if user is None:
                user = User(
                    github_id=int(profile["id"]),
                    github_username=profile["login"],
                    display_name=profile.get("name") or profile["login"],
                    avatar_url=profile.get("avatar_url") or "",
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
            else:
                user.github_username = profile["login"]
                user.display_name = profile.get("name") or profile["login"]
                user.avatar_url = profile.get("avatar_url") or ""
                user.updated_at = now
            return user.id

    def create_user_session(
        self,
        user_id: int,
        session_hash: str,
        csrf_hash: str,
        encrypted_access_token: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            session.execute(delete(UserSession).where(UserSession.expires_at <= created_at))
            session.add(
                UserSession(
                    user_id=user_id,
                    session_hash=session_hash,
                    csrf_hash=csrf_hash,
                    encrypted_access_token=encrypted_access_token,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )

    def get_user_session(self, session_hash: str, now: datetime) -> dict[str, Any] | None:
        self.initialize()
        statement = (
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.session_hash == session_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )
        with Session(self.engine) as session:
            row = session.execute(statement).first()
        if row is None:
            return None
        user_session, user = row
        return {
            "user_id": user.id,
            "github_id": user.github_id,
            "github_username": user.github_username,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "encrypted_access_token": user_session.encrypted_access_token,
            "session_hash": user_session.session_hash,
            "csrf_hash": user_session.csrf_hash,
            "expires_at": user_session.expires_at,
        }

    def revoke_user_session(self, session_hash: str, now: datetime) -> bool:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            user_session = session.scalar(
                select(UserSession).where(UserSession.session_hash == session_hash)
            )
            if user_session is None or user_session.revoked_at is not None:
                return False
            user_session.revoked_at = now
            session.add(
                AuditLog(
                    user_id=user_session.user_id,
                    action="auth.logout",
                    outcome="revoked",
                    created_at=now,
                )
            )
            return True

    def record_audit_event(
        self,
        *,
        user_id: int | None,
        action: str,
        created_at: datetime,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
    ) -> None:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            session.add(
                AuditLog(
                    user_id=user_id,
                    action=action,
                    repository_owner=owner,
                    repository_name=repo,
                    pull_number=pull_number,
                    resource_id=resource_id,
                    outcome=outcome,
                    created_at=created_at,
                )
            )

    def record_repository_access(
        self,
        user_id: int,
        owner: str,
        repo: str,
        permission: str,
        now: datetime,
    ) -> None:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            repository = self._upsert_repository(session, owner, repo, now)
            access = session.scalar(
                select(RepositoryAccess).where(
                    RepositoryAccess.user_id == user_id,
                    RepositoryAccess.repository_id == repository.id,
                )
            )
            if access is None:
                access = RepositoryAccess(
                    user_id=user_id,
                    repository_id=repository.id,
                    permission=permission,
                    verified_at=now,
                )
                session.add(access)
            else:
                access.permission = permission
                access.verified_at = now

    def create_fix_approval(
        self,
        *,
        fix_proposal_id: int,
        user_id: int,
        repository_id: int,
        pull_number: int,
        head_sha: str,
        approved_at: datetime,
        expires_at: datetime,
    ) -> int:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            approval = FixApproval(
                fix_proposal_id=fix_proposal_id,
                user_id=user_id,
                repository_id=repository_id,
                pull_number=pull_number,
                head_sha=head_sha,
                approved_at=approved_at,
                expires_at=expires_at,
            )
            session.add(approval)
            session.flush()
            return approval.id

    def get_fix_approval(self, approval_id: int, user_id: int) -> dict[str, Any] | None:
        self.initialize()
        statement = (
            select(
                FixApproval.id,
                FixApproval.user_id,
                FixApproval.repository_id,
                FixApproval.pull_number,
                FixApproval.head_sha,
                FixApproval.approved_at,
                FixApproval.expires_at,
                FixApproval.consumed_at,
                Repository.owner,
                Repository.name.label("repo"),
                FixProposal.file_path,
                FixProposal.updated_code,
                ReviewHistory.head_sha.label("proposal_head_sha"),
            )
            .join(Repository, Repository.id == FixApproval.repository_id)
            .join(FixProposal, FixProposal.id == FixApproval.fix_proposal_id)
            .join(ReviewHistory, ReviewHistory.id == FixProposal.review_id)
            .join(PullRequest, PullRequest.id == ReviewHistory.pull_request_id)
            .where(FixApproval.id == approval_id, FixApproval.user_id == user_id)
            .where(
                FixApproval.repository_id == PullRequest.repository_id,
                FixApproval.pull_number == PullRequest.number,
            )
        )
        with Session(self.engine) as session:
            row = session.execute(statement).mappings().first()
        if row is None:
            return None
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }

    def get_fix_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        self.initialize()
        statement = (
            select(
                FixProposal.id.label("fix_proposal_id"),
                FixProposal.file_path,
                FixProposal.updated_code,
                ReviewHistory.head_sha,
                ReviewHistory.user_id,
                PullRequest.number.label("pull_number"),
                PullRequest.head_sha.label("current_head_sha"),
                Repository.id.label("repository_id"),
                Repository.owner,
                Repository.name.label("repo"),
            )
            .join(ReviewHistory, ReviewHistory.id == FixProposal.review_id)
            .join(PullRequest, PullRequest.id == ReviewHistory.pull_request_id)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .where(FixProposal.id == proposal_id)
        )
        with Session(self.engine) as session:
            row = session.execute(statement).mappings().first()
        return dict(row) if row is not None else None

    def consume_fix_approval(
        self,
        approval_id: int,
        user_id: int,
        now: datetime,
    ) -> bool:
        self.initialize()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                FixApproval.__table__.update()
                .where(
                    FixApproval.id == approval_id,
                    FixApproval.user_id == user_id,
                    FixApproval.expires_at > now,
                    FixApproval.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            return result.rowcount == 1

    def register_webhook_job(self, job_id: str, event: dict[str, Any]) -> None:
        self.initialize()
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session, session.begin():
            repository = self._upsert_repository(session, event["owner"], event["repo"], now)
            self._upsert_pull_request(
                session,
                repository,
                event["pull_number"],
                event["head_sha"],
                now,
            )
            insert = self._dialect_insert(session, WebhookJob)
            session.execute(
                insert(
                    job_id=job_id,
                    event_id=self.event_id(event),
                    repository_id=repository.id,
                    pull_number=event["pull_number"],
                    head_sha=event["head_sha"],
                    action=event["action"],
                    status="queued",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )

    @staticmethod
    def event_id(event: dict[str, Any]) -> str:
        return f"{event['owner'].lower()}/{event['repo'].lower()}:{event['pull_number']}:{event['head_sha']}"

    def update_job(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        self.initialize()
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session, session.begin():
            job = session.get(WebhookJob, job_id)
            if job is None:
                return
            job.status = status
            job.last_error = error
            job.updated_at = now
            if increment_attempts:
                job.attempts += 1
                job.started_at = now
            if status in {"completed", "failed"}:
                job.finished_at = now
            elif status in {"queued", "retrying", "started"}:
                job.finished_at = None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        statement = (
            select(
                WebhookJob.job_id,
                WebhookJob.event_id,
                Repository.owner,
                Repository.name.label("repo"),
                WebhookJob.pull_number,
                WebhookJob.head_sha,
                WebhookJob.action,
                WebhookJob.status,
                WebhookJob.attempts,
                WebhookJob.last_error,
                WebhookJob.created_at,
                WebhookJob.updated_at,
                WebhookJob.started_at,
                WebhookJob.finished_at,
            )
            .join(Repository, Repository.id == WebhookJob.repository_id)
            .where(WebhookJob.job_id == job_id)
        )
        with Session(self.engine) as session:
            row = session.execute(statement).mappings().first()
        if row is None:
            return None
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }

    def save_review_result(
        self,
        event: dict[str, Any],
        result: dict[str, Any],
        *,
        job_id: str | None = None,
        user_id: int | None = None,
    ) -> int | None:
        review = result.get("review")
        if not isinstance(review, dict):
            return None

        self.initialize()
        now = datetime.now(timezone.utc)
        pull_request_data = result.get("pull_request") or {}
        publication = result.get("publication") or {}
        head_sha = pull_request_data.get("head_sha") or event["head_sha"]

        with Session(self.engine) as session, session.begin():
            repository = self._upsert_repository(session, event["owner"], event["repo"], now)
            pull_request = self._upsert_pull_request(
                session,
                repository,
                event["pull_number"],
                head_sha,
                now,
                title=pull_request_data.get("title", ""),
                state=pull_request_data.get("state", ""),
                html_url=pull_request_data.get("html_url", ""),
            )
            history = (
                session.scalar(select(ReviewHistory).where(ReviewHistory.job_id == job_id))
                if job_id is not None
                else None
            )
            if history is None:
                history = ReviewHistory(
                    job_id=job_id,
                    user_id=user_id,
                    pull_request_id=pull_request.id,
                    head_sha=head_sha,
                    summary=review.get("summary", ""),
                    status=result.get("status", "review_completed"),
                    publication_status=publication.get("status", "not_attempted"),
                    publication_id=publication.get("review_id"),
                    publication_url=publication.get("html_url"),
                    created_at=now,
                    updated_at=now,
                )
                session.add(history)
                session.flush()
            else:
                history.user_id = user_id
                history.pull_request_id = pull_request.id
                history.head_sha = head_sha
                history.summary = review.get("summary", "")
                history.status = result.get("status", "review_completed")
                history.publication_status = publication.get("status", "not_attempted")
                history.publication_id = publication.get("review_id")
                history.publication_url = publication.get("html_url")
                history.updated_at = now
                session.execute(
                    delete(ReviewFinding).where(ReviewFinding.review_id == history.id)
                )
                session.execute(
                    delete(FixProposal).where(FixProposal.review_id == history.id)
                )

            for finding in review.get("findings", []):
                session.add(
                    ReviewFinding(
                        review_id=history.id,
                        category=finding["category"],
                        severity=finding["severity"],
                        title=finding["title"],
                        description=finding["description"],
                        evidence=finding["evidence"],
                        suggested_fix=finding["suggested_fix"],
                        file_path=finding["file_path"],
                        line_start=finding["line_start"],
                        line_end=finding["line_end"],
                        confidence=finding["confidence"],
                        created_at=now,
                    )
                )

            fix = result.get("fix")
            if isinstance(fix, dict) and fix.get("file_path"):
                session.add(
                    FixProposal(
                        review_id=history.id,
                        file_path=fix["file_path"],
                        explanation=fix.get("explanation", ""),
                        updated_code=fix.get("updated_code", ""),
                        patch=fix.get("patch", ""),
                        tests_to_add=json.dumps(fix.get("tests_to_add", [])),
                        assumptions=json.dumps(fix.get("assumptions", [])),
                        structural_validation=json.dumps(
                            fix.get("structural_validation", {})
                        ),
                        publication_status="not_attempted",
                        created_at=now,
                        updated_at=now,
                    )
                )
            return history.id

    def get_fix_proposal_id(self, review_id: int) -> int | None:
        self.initialize()
        statement = (
            select(FixProposal.id)
            .where(FixProposal.review_id == review_id)
            .order_by(FixProposal.id.desc())
            .limit(1)
        )
        with Session(self.engine) as session:
            return session.scalar(statement)

    def list_user_review_history(self, user_id: int) -> list[dict[str, Any]]:
        self.initialize()
        statement = (
            select(
                ReviewHistory.id,
                Repository.owner,
                Repository.name.label("repo"),
                PullRequest.number.label("pull_number"),
                ReviewHistory.head_sha,
                ReviewHistory.summary,
                ReviewHistory.status,
                ReviewHistory.publication_status,
                ReviewHistory.created_at,
                FixProposal.id.label("fix_proposal_id"),
                FixProposal.file_path,
            )
            .join(PullRequest, PullRequest.id == ReviewHistory.pull_request_id)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .outerjoin(FixProposal, FixProposal.review_id == ReviewHistory.id)
            .where(ReviewHistory.user_id == user_id)
            .order_by(ReviewHistory.created_at.desc(), ReviewHistory.id.desc())
            .limit(100)
        )
        with Session(self.engine) as session:
            rows = session.execute(statement).mappings().all()
        return [
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in row.items()
            }
            for row in rows
        ]

    def update_review_publication(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        head_sha: str,
        publication: dict[str, Any],
        *,
        user_id: int | None = None,
    ) -> None:
        self.initialize()
        statement = (
            select(ReviewHistory)
            .join(PullRequest, PullRequest.id == ReviewHistory.pull_request_id)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .where(
                Repository.owner == owner,
                Repository.name == repo,
                PullRequest.number == pull_number,
                ReviewHistory.head_sha == head_sha,
            )
            .order_by(ReviewHistory.id.desc())
            .limit(1)
        )
        if user_id is not None:
            statement = statement.where(ReviewHistory.user_id == user_id)
        with Session(self.engine) as session, session.begin():
            history = session.scalar(statement)
            if history is not None:
                history.publication_status = publication.get("status", "unknown")
                history.publication_id = publication.get("review_id")
                history.publication_url = publication.get("html_url")
                history.updated_at = datetime.now(timezone.utc)

    def update_fix_publication(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        head_sha: str,
        file_path: str,
        publication: dict[str, Any],
    ) -> None:
        self.initialize()
        statement = (
            select(FixProposal)
            .join(ReviewHistory, ReviewHistory.id == FixProposal.review_id)
            .join(PullRequest, PullRequest.id == ReviewHistory.pull_request_id)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .where(
                Repository.owner == owner,
                Repository.name == repo,
                PullRequest.number == pull_number,
                ReviewHistory.head_sha == head_sha,
                FixProposal.file_path == file_path,
            )
            .order_by(FixProposal.id.desc())
            .limit(1)
        )
        with Session(self.engine) as session, session.begin():
            proposal = session.scalar(statement)
            if proposal is not None:
                proposal.publication_status = publication.get("status", "unknown")
                proposal.commit_sha = publication.get("commit_sha")
                proposal.commit_url = publication.get("commit_url")
                proposal.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _upsert_repository(
        session: Session, owner: str, repo: str, now: datetime
    ) -> Repository:
        insert = ReviewDatabase._dialect_insert(session, Repository)
        session.execute(
            insert(
                owner=owner,
                name=repo,
                created_at=now,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=["owner", "name"],
                set_={"updated_at": now},
            )
        )
        return session.scalar(
            select(Repository).where(
                Repository.owner == owner,
                Repository.name == repo,
            )
        )

    @staticmethod
    def _upsert_pull_request(
        session: Session,
        repository: Repository,
        number: int,
        head_sha: str,
        now: datetime,
        *,
        title: str = "",
        state: str = "",
        html_url: str = "",
    ) -> PullRequest:
        insert = ReviewDatabase._dialect_insert(session, PullRequest)
        updates = {
            "head_sha": head_sha,
            "updated_at": now,
        }
        if title:
            updates["title"] = title
        if state:
            updates["state"] = state
        if html_url:
            updates["html_url"] = html_url
        session.execute(
            insert(
                repository_id=repository.id,
                number=number,
                title=title,
                state=state,
                head_sha=head_sha,
                html_url=html_url,
                created_at=now,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=["repository_id", "number"],
                set_=updates,
            )
        )
        return session.scalar(
            select(PullRequest).where(
                PullRequest.repository_id == repository.id,
                PullRequest.number == number,
            )
        )

    @staticmethod
    def _dialect_insert(session: Session, model):
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            return lambda **values: postgresql_insert(model).values(**values)
        if dialect_name == "sqlite":
            return lambda **values: sqlite_insert(model).values(**values)
        raise RuntimeError(f"Unsupported database dialect: {dialect_name}")

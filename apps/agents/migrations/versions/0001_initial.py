"""Create persistent review and job history tables.

Revision ID: 0001_initial
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner", "name", name="uq_repository_owner_name"),
    )
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("html_url", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repository_id", "number", name="uq_pull_request_repository_number"),
    )
    op.create_index(
        "ix_pull_requests_repository_updated",
        "pull_requests",
        ["repository_id", "updated_at"],
    )
    op.create_table(
        "webhook_jobs",
        sa.Column("job_id", sa.String(length=100), primary_key=True),
        sa.Column("event_id", sa.String(length=300), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("pull_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", name="uq_webhook_job_event"),
    )
    op.create_index(
        "ix_webhook_jobs_status_created",
        "webhook_jobs",
        ["status", "created_at"],
    )
    op.create_table(
        "review_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.String(length=100)),
        sa.Column("pull_request_id", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("publication_status", sa.String(length=50), nullable=False),
        sa.Column("publication_id", sa.String(length=100)),
        sa.Column("publication_url", sa.String(length=1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["webhook_jobs.job_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_review_history_pull_request_created",
        "review_history",
        ["pull_request_id", "created_at"],
    )
    op.create_index("ix_review_history_head_sha", "review_history", ["head_sha"])
    op.create_table(
        "review_findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["review_history.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_review_findings_review_severity",
        "review_findings",
        ["review_id", "severity"],
    )
    op.create_table(
        "fix_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("updated_code", sa.Text(), nullable=False),
        sa.Column("patch", sa.Text(), nullable=False),
        sa.Column("tests_to_add", sa.Text(), nullable=False),
        sa.Column("assumptions", sa.Text(), nullable=False),
        sa.Column("structural_validation", sa.Text(), nullable=False),
        sa.Column("publication_status", sa.String(length=50), nullable=False),
        sa.Column("commit_sha", sa.String(length=64)),
        sa.Column("commit_url", sa.String(length=1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["review_history.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("review_id", "file_path", name="uq_fix_proposal_review_file"),
    )
    op.create_index("ix_fix_proposals_review", "fix_proposals", ["review_id"])


def downgrade() -> None:
    op.drop_index("ix_fix_proposals_review", table_name="fix_proposals")
    op.drop_table("fix_proposals")
    op.drop_index("ix_review_findings_review_severity", table_name="review_findings")
    op.drop_table("review_findings")
    op.drop_index("ix_review_history_head_sha", table_name="review_history")
    op.drop_index(
        "ix_review_history_pull_request_created",
        table_name="review_history",
    )
    op.drop_table("review_history")
    op.drop_index("ix_webhook_jobs_status_created", table_name="webhook_jobs")
    op.drop_table("webhook_jobs")
    op.drop_index("ix_pull_requests_repository_updated", table_name="pull_requests")
    op.drop_table("pull_requests")
    op.drop_table("repositories")

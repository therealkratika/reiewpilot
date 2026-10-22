"""Persist users, authenticated sessions, repository access, and fix approvals.

Revision ID: 0002_auth_sessions
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_auth_sessions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_username", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("avatar_url", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("github_id", name="uq_users_github_id"),
        sa.UniqueConstraint("github_username", name="uq_users_github_username"),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("session_hash", name="uq_user_sessions_session_hash"),
    )
    op.create_index(
        "ix_user_sessions_user_expires",
        "user_sessions",
        ["user_id", "expires_at"],
    )
    op.create_table(
        "repository_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.String(length=30), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "repository_id",
            name="uq_repository_access_user_repo",
        ),
    )
    op.create_index(
        "ix_repository_access_repository",
        "repository_access",
        ["repository_id"],
    )
    op.add_column(
        "review_history",
        sa.Column("user_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_review_history_user_created",
        "review_history",
        ["user_id", "created_at"],
    )
    op.create_table(
        "fix_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fix_proposal_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("pull_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["fix_proposal_id"],
            ["fix_proposals.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_fix_approvals_proposal_user",
        "fix_approvals",
        ["fix_proposal_id", "user_id"],
    )
    op.create_index(
        "ix_fix_approvals_expires",
        "fix_approvals",
        ["expires_at"],
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("repository_owner", sa.String(length=100)),
        sa.Column("repository_name", sa.String(length=100)),
        sa.Column("pull_number", sa.Integer()),
        sa.Column("resource_id", sa.String(length=200)),
        sa.Column("outcome", sa.String(length=50)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_audit_logs_user_created",
        "audit_logs",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_action_created",
        "audit_logs",
        ["action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_fix_approvals_expires", table_name="fix_approvals")
    op.drop_index("ix_fix_approvals_proposal_user", table_name="fix_approvals")
    op.drop_table("fix_approvals")
    op.drop_index("ix_review_history_user_created", table_name="review_history")
    op.drop_column("review_history", "user_id")
    op.drop_index("ix_repository_access_repository", table_name="repository_access")
    op.drop_table("repository_access")
    op.drop_index("ix_user_sessions_user_expires", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("oauth_states")
    op.drop_table("users")

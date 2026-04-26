"""schema fixes: jti blocklist, repo_jobs table, nullable constraints

Revision ID: b2c4d1e3f5a7
Revises: 8f1573050ef8
Create Date: 2026-04-26 18:00:00.000000

Changes:
- token_blocklist: rename 'token' column to 'jti' (stores UUID, not full JWT)
- Create repo_jobs table (was missing from initial migration)
- users.email / users.password: add NOT NULL constraints
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4d1e3f5a7"
down_revision: Union[str, Sequence[str], None] = "8f1573050ef8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. token_blocklist: replace full-token PK with jti (UUID) ─────────────
    # Drop old table and recreate — simpler than ALTER on a PK column.
    # Any existing revoked tokens are lost on upgrade; that is acceptable
    # (tokens will expire naturally via their exp claim).
    op.drop_table("token_blocklist")
    op.create_table(
        "token_blocklist",
        sa.Column("jti", sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint("jti"),
    )

    # ── 2. repo_jobs (was missing from initial migration) ─────────────────────
    op.create_table(
        "repo_jobs",
        sa.Column("id", sa.String(), nullable=False),       # celery task_id
        sa.Column("repo_name", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),   # PENDING/CLONING/DONE/FAILED
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repo_jobs_id", "repo_jobs", ["id"], unique=False)
    op.create_index("ix_repo_jobs_user_id", "repo_jobs", ["user_id"], unique=False)

    # ── 3. users: tighten nullable constraints ────────────────────────────────
    # First fill any existing NULLs to avoid constraint violation
    op.execute("UPDATE users SET email = '' WHERE email IS NULL")
    op.execute("UPDATE users SET password = '' WHERE password IS NULL")
    op.alter_column("users", "email", existing_type=sa.String(), nullable=False)
    op.alter_column("users", "password", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    # ── 3. Revert users nullable constraints ──────────────────────────────────
    op.alter_column("users", "password", existing_type=sa.String(), nullable=True)
    op.alter_column("users", "email", existing_type=sa.String(), nullable=True)

    # ── 2. Drop repo_jobs ─────────────────────────────────────────────────────
    op.drop_index("ix_repo_jobs_user_id", table_name="repo_jobs")
    op.drop_index("ix_repo_jobs_id", table_name="repo_jobs")
    op.drop_table("repo_jobs")

    # ── 1. Restore old token_blocklist (full token as PK) ─────────────────────
    op.drop_table("token_blocklist")
    op.create_table(
        "token_blocklist",
        sa.Column("token", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("token"),
    )

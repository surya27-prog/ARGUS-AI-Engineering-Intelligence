"""add cochange columns to parse_jobs

Revision ID: fccc7b331f0f
Revises: 4c5f7660f317
Create Date: 2026-08-17 17:02:20.698605

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'fccc7b331f0f'
down_revision: str | None = '4c5f7660f317'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default for the same reason as the embedding columns: parse_jobs
    # already holds rows from earlier weeks and Postgres needs a value for them.
    # Zero is also the honest value there — those runs read no history.
    op.add_column(
        'parse_jobs',
        sa.Column('commits_analyzed', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'parse_jobs',
        sa.Column('cochange_edges', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('parse_jobs', 'cochange_edges')
    op.drop_column('parse_jobs', 'commits_analyzed')

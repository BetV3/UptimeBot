"""add ssl monitor fields

Revision ID: d5f2c1a8e304
Revises: c8d3a5e7b921
Create Date: 2026-04-12 11:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'd5f2c1a8e304'
down_revision: Union[str, None] = 'c8d3a5e7b921'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extend the monitortype enum with 'ssl'
    op.execute("ALTER TYPE monitortype ADD VALUE IF NOT EXISTS 'ssl'")

    op.add_column('monitors', sa.Column('target_host', sa.String(length=255), nullable=True))
    op.add_column('monitors', sa.Column('target_port', sa.Integer(), nullable=True))
    op.add_column('monitors', sa.Column('warn_days_before_expiry', sa.Integer(), nullable=True))

    op.add_column('checks', sa.Column('cert_days_remaining', sa.Integer(), nullable=True))
    op.add_column('checks', sa.Column('cert_subject', sa.String(length=512), nullable=True))
    op.add_column('checks', sa.Column('cert_issuer', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('checks', 'cert_issuer')
    op.drop_column('checks', 'cert_subject')
    op.drop_column('checks', 'cert_days_remaining')

    op.drop_column('monitors', 'warn_days_before_expiry')
    op.drop_column('monitors', 'target_port')
    op.drop_column('monitors', 'target_host')
    # Postgres cannot drop enum values; leaving 'ssl' in the type is safe.

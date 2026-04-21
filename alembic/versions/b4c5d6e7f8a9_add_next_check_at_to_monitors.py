"""add last_checked_at and next_check_at to monitors

Revision ID: b4c5d6e7f8a9
Revises: a3f8b2e91c47
Create Date: 2026-04-20 23:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, None] = 'a3f8b2e91c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('monitors', sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('monitors', sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True))

    # Backfill: make every active monitor immediately due so the new scheduler
    # picks them up on its next tick. Inactive monitors stay NULL.
    op.execute("UPDATE monitors SET next_check_at = now() WHERE is_active = true")

    op.create_index(
        'ix_monitors_next_check_at',
        'monitors',
        ['next_check_at'],
        postgresql_where=sa.text('is_active = true'),
    )


def downgrade() -> None:
    op.drop_index('ix_monitors_next_check_at', table_name='monitors')
    op.drop_column('monitors', 'next_check_at')
    op.drop_column('monitors', 'last_checked_at')

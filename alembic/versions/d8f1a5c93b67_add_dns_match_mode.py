"""add dns_match_mode to monitors

Revision ID: d8f1a5c93b67
Revises: c4a9e6b03175
Create Date: 2026-05-19 21:00:00.000000

Adds a per-monitor DNS matching mode. "all" preserves the existing strict
behavior (every expected value must appear in the resolved answer). "any"
flips it to loose (at least one expected value must appear), which is what
you want for round-robin third-party records whose answer set rotates.

Existing rows default to "all" so behavior is unchanged after upgrade.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'd8f1a5c93b67'
down_revision: Union[str, None] = 'c4a9e6b03175'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dns_match_mode = sa.Enum('all', 'any', name='dnsmatchmode')
    dns_match_mode.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'monitors',
        sa.Column(
            'dns_match_mode',
            dns_match_mode,
            nullable=False,
            server_default='all',
        ),
    )


def downgrade() -> None:
    op.drop_column('monitors', 'dns_match_mode')
    sa.Enum(name='dnsmatchmode').drop(op.get_bind(), checkfirst=True)

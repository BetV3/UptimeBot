"""add type column to monitors

Revision ID: c8d3a5e7b921
Revises: b7e4f1a29c10
Create Date: 2026-04-12 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'c8d3a5e7b921'
down_revision: Union[str, None] = 'b7e4f1a29c10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    monitor_type = sa.Enum('http', name='monitortype')
    monitor_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'monitors',
        sa.Column('type', monitor_type, nullable=False, server_default='http'),
    )


def downgrade() -> None:
    op.drop_column('monitors', 'type')
    sa.Enum(name='monitortype').drop(op.get_bind(), checkfirst=True)

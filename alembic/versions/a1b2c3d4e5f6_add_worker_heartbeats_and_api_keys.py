"""add worker_heartbeats and api_keys tables

Revision ID: a1b2c3d4e5f6
Revises: 484088076986
Create Date: 2026-03-25 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '484088076986'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('worker_heartbeats',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('region', postgresql.ENUM('US', 'EU', 'ASIA', name='checkregion', create_type=False), nullable=False),
        sa.Column('hostname', sa.String(length=255), nullable=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('version', sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('region'),
    )

    op.create_table('api_keys',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('key_hash', sa.String(length=255), nullable=False),
        sa.Column('prefix', sa.String(length=8), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_api_keys_prefix'), 'api_keys', ['prefix'], unique=False)

    # Add slack and webhook alert types to the existing enum
    op.execute("ALTER TYPE alerttype ADD VALUE IF NOT EXISTS 'slack'")
    op.execute("ALTER TYPE alerttype ADD VALUE IF NOT EXISTS 'webhook'")


def downgrade() -> None:
    op.drop_index(op.f('ix_api_keys_prefix'), table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_table('worker_heartbeats')
    # Note: Cannot remove enum values in PostgreSQL

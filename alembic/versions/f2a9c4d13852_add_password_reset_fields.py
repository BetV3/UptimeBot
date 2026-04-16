"""add password reset fields to users

Revision ID: f2a9c4d13852
Revises: e1f3b9d72541
Create Date: 2026-04-15 01:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'f2a9c4d13852'
down_revision: Union[str, None] = 'e1f3b9d72541'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('password_reset_token', sa.String(length=128), nullable=True))
    op.add_column('users', sa.Column('password_reset_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_users_password_reset_token', 'users', ['password_reset_token'])


def downgrade() -> None:
    op.drop_index('ix_users_password_reset_token', table_name='users')
    op.drop_column('users', 'password_reset_expires_at')
    op.drop_column('users', 'password_reset_token')

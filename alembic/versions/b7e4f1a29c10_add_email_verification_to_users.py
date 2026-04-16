"""add email verification fields to users

Revision ID: b7e4f1a29c10
Revises: a1b2c3d4e5f6
Create Date: 2026-04-11 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4f1a29c10'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('email_verified', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column(
        'users',
        sa.Column('verification_token', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('verification_token_expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f('ix_users_verification_token'),
        'users',
        ['verification_token'],
        unique=False,
    )

    # Treat any pre-existing accounts as verified so we don't lock them out.
    op.execute("UPDATE users SET email_verified = true")


def downgrade() -> None:
    op.drop_index(op.f('ix_users_verification_token'), table_name='users')
    op.drop_column('users', 'verification_token_expires_at')
    op.drop_column('users', 'verification_token')
    op.drop_column('users', 'email_verified')

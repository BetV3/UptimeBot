"""add stripe billing fields to users

Revision ID: a3f8b2e91c47
Revises: f2a9c4d13852
Create Date: 2026-04-16 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'a3f8b2e91c47'
down_revision: Union[str, None] = 'f2a9c4d13852'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUBSCRIPTION_STATUS_VALUES = (
    'active',
    'trialing',
    'past_due',
    'canceled',
    'incomplete',
    'incomplete_expired',
    'unpaid',
)


def upgrade() -> None:
    subscription_status = sa.Enum(*SUBSCRIPTION_STATUS_VALUES, name='subscriptionstatus')
    subscription_status.create(op.get_bind(), checkfirst=True)

    op.add_column('users', sa.Column('stripe_customer_id', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('stripe_subscription_id', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('subscription_status', subscription_status, nullable=True))
    op.add_column('users', sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_users_stripe_customer_id', 'users', ['stripe_customer_id'])
    op.create_index('ix_users_stripe_subscription_id', 'users', ['stripe_subscription_id'])


def downgrade() -> None:
    op.drop_index('ix_users_stripe_subscription_id', table_name='users')
    op.drop_index('ix_users_stripe_customer_id', table_name='users')
    op.drop_column('users', 'current_period_end')
    op.drop_column('users', 'subscription_status')
    op.drop_column('users', 'stripe_subscription_id')
    op.drop_column('users', 'stripe_customer_id')

    sa.Enum(name='subscriptionstatus').drop(op.get_bind(), checkfirst=True)

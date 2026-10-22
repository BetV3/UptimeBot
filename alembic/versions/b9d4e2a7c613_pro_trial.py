"""pro trial: 14-day Pro trial with no card, and why a monitor is paused

Revision ID: b9d4e2a7c613
Revises: e3c1a7f05d92
Create Date: 2026-10-22 12:00:00.000000

users.trial_ends_at        when the trial ends (NULL: no trial running or it was
                           cleared by a paid subscription)
users.trial_used           one trial per account, ever
users.trial_offer          signed up from the site check: offer the trial on the
                           first project screen
users.trial_reminder_sent_at  the single "ends in 3 days" email went out
users.trial_expired_at     the hourly task already moved this account back to Free
monitors.paused_reason     NULL for a pause the user chose; "trial_ended" or
                           "plan_limit" when the plan paused it, so an upgrade
                           can resume exactly those
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9d4e2a7c613"
down_revision: Union[str, None] = "e3c1a7f05d92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the trial columns and monitors.paused_reason."""
    op.add_column("users", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("trial_offer", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("trial_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("trial_expired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_trial_ends_at", "users", ["trial_ends_at"])
    op.add_column("monitors", sa.Column("paused_reason", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Drop them again. Paused monitors stay paused (is_active is untouched)."""
    op.drop_column("monitors", "paused_reason")
    op.drop_index("ix_users_trial_ends_at", table_name="users")
    op.drop_column("users", "trial_expired_at")
    op.drop_column("users", "trial_reminder_sent_at")
    op.drop_column("users", "trial_offer")
    op.drop_column("users", "trial_used")
    op.drop_column("users", "trial_ends_at")

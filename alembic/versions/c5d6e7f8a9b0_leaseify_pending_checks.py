"""lease-ify pending_checks

Replaces the one-way claimed_at flag with a proper lease:
  leased_at / lease_expires_at / worker_id / attempts / dead.

Expired leases can be re-claimed by any worker; attempts-capped rows are
marked dead by the reap_dead_pending_checks sweeper so the scheduler can
insert a fresh one for the next interval.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-04-20 23:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pending_checks', sa.Column('leased_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('pending_checks', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('pending_checks', sa.Column('worker_id', sa.String(length=255), nullable=True))
    op.add_column('pending_checks', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('pending_checks', sa.Column('dead', sa.Boolean(), nullable=False, server_default=sa.text('false')))

    # Retire any previously claimed rows so the new unique partial index can be
    # created without conflicts. These rows already had results posted (the
    # old /internal/results handler set claimed_at as the "done" marker).
    op.execute("UPDATE pending_checks SET dead = true WHERE claimed_at IS NOT NULL")

    # Also retire any unclaimed rows older than 10 minutes — they predate the
    # new lease model and could duplicate against fresh scheduler inserts.
    op.execute("UPDATE pending_checks SET dead = true WHERE claimed_at IS NULL AND scheduled_at < now() - interval '10 minutes'")

    op.drop_column('pending_checks', 'claimed_at')

    op.create_index(
        'uq_pending_checks_monitor_region_live',
        'pending_checks',
        ['monitor_id', 'region'],
        unique=True,
        postgresql_where=sa.text('dead = false'),
    )

    # Secondary index for the sweeper and /internal/jobs eligibility scan.
    op.create_index(
        'ix_pending_checks_region_lease',
        'pending_checks',
        ['region', 'lease_expires_at'],
        postgresql_where=sa.text('dead = false'),
    )


def downgrade() -> None:
    op.drop_index('ix_pending_checks_region_lease', table_name='pending_checks')
    op.drop_index('uq_pending_checks_monitor_region_live', table_name='pending_checks')

    op.add_column('pending_checks', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE pending_checks SET claimed_at = leased_at WHERE leased_at IS NOT NULL")

    op.drop_column('pending_checks', 'dead')
    op.drop_column('pending_checks', 'attempts')
    op.drop_column('pending_checks', 'worker_id')
    op.drop_column('pending_checks', 'lease_expires_at')
    op.drop_column('pending_checks', 'leased_at')

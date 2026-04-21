"""add alert_deliveries table

Durable per-channel alert delivery record so Celery workers can retry sends
without re-firing duplicates. The (incident_id, alert_channel_id, kind)
unique index is the idempotency key — an INSERT ON CONFLICT DO NOTHING from
/internal/results is what the celery task picks up.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-04-21 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'alert_deliveries',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('incident_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('incidents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('alert_channel_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('alert_channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        'uq_alert_deliveries_incident_channel_kind',
        'alert_deliveries',
        ['incident_id', 'alert_channel_id', 'kind'],
        unique=True,
    )

    # Worker scan: grab pending rows in creation order.
    op.create_index(
        'ix_alert_deliveries_state_created',
        'alert_deliveries',
        ['state', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_alert_deliveries_state_created', table_name='alert_deliveries')
    op.drop_index('uq_alert_deliveries_incident_channel_kind', table_name='alert_deliveries')
    op.drop_table('alert_deliveries')

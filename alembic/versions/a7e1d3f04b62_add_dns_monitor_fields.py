"""add dns monitor fields

Revision ID: a7e1d3f04b62
Revises: d6e7f8a9b0c1
Create Date: 2026-05-17 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'a7e1d3f04b62'
down_revision: Union[str, None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE monitortype ADD VALUE IF NOT EXISTS 'DNS'")

    dns_record_type = sa.Enum(
        'A', 'AAAA', 'CNAME', 'MX', 'TXT', 'NS', name='dnsrecordtype'
    )
    dns_record_type.create(op.get_bind(), checkfirst=True)

    op.add_column('monitors', sa.Column('dns_record_type', dns_record_type, nullable=True))
    op.add_column('monitors', sa.Column('dns_expected_value', sa.Text(), nullable=True))
    op.add_column('monitors', sa.Column('dns_resolver', sa.String(length=255), nullable=True))

    op.add_column('checks', sa.Column('dns_resolved_values', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('checks', 'dns_resolved_values')

    op.drop_column('monitors', 'dns_resolver')
    op.drop_column('monitors', 'dns_expected_value')
    op.drop_column('monitors', 'dns_record_type')

    sa.Enum(name='dnsrecordtype').drop(op.get_bind(), checkfirst=True)
    # Postgres cannot drop enum values; leaving 'DNS' in monitortype is safe.

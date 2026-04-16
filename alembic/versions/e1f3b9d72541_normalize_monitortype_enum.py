"""normalize monitortype + alerttype enums to upper case

Revision ID: e1f3b9d72541
Revises: d5f2c1a8e304
Create Date: 2026-04-15 00:00:00.000000

The earlier migrations seeded ``monitortype`` and parts of ``alerttype``
with lowercase values, but every other enum in the schema follows
SQLAlchemy's default convention of storing the enum *name* (upper
case). The mismatch made monitor inserts raise
``invalid input value for enum monitortype: "HTTP"`` and channel
inserts raise the same for ``slack``/``webhook`` alert types.
"""
from typing import Sequence, Union
from alembic import op


revision: str = 'e1f3b9d72541'
down_revision: Union[str, None] = 'd5f2c1a8e304'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE monitors ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE monitortype RENAME VALUE 'http' TO 'HTTP'")
    op.execute("ALTER TYPE monitortype RENAME VALUE 'ssl' TO 'SSL'")
    op.execute("ALTER TABLE monitors ALTER COLUMN type SET DEFAULT 'HTTP'")

    op.execute("ALTER TYPE alerttype RENAME VALUE 'slack' TO 'SLACK'")
    op.execute("ALTER TYPE alerttype RENAME VALUE 'webhook' TO 'WEBHOOK'")


def downgrade() -> None:
    op.execute("ALTER TYPE alerttype RENAME VALUE 'WEBHOOK' TO 'webhook'")
    op.execute("ALTER TYPE alerttype RENAME VALUE 'SLACK' TO 'slack'")

    op.execute("ALTER TABLE monitors ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE monitortype RENAME VALUE 'HTTP' TO 'http'")
    op.execute("ALTER TYPE monitortype RENAME VALUE 'SSL' TO 'ssl'")
    op.execute("ALTER TABLE monitors ALTER COLUMN type SET DEFAULT 'http'")

"""normalize checkregion enum to lower case

Revision ID: c4a9e6b03175
Revises: a7e1d3f04b62
Create Date: 2026-05-19 00:00:00.000000

The initial migration seeded ``checkregion`` with SQLAlchemy's default
upper-case names (``US``, ``EU``, ``ASIA``), but the FastAPI surface
and worker both use the enum's lower-case ``.value`` (``us``, ``eu``,
``asia``). The mismatch made ``/internal/jobs?region=us`` fail with
``invalid input value for enum checkregion: "us"`` and forced callers
to choose between a 500 (lower-case) and a 422 (upper-case). Same
fix shape as ``e1f3b9d72541``, applied in the other direction so the
DB labels line up with ``CheckRegion.value``.
"""
from typing import Sequence, Union
from alembic import op


revision: str = 'c4a9e6b03175'
down_revision: Union[str, None] = 'a7e1d3f04b62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE checkregion RENAME VALUE 'US' TO 'us'")
    op.execute("ALTER TYPE checkregion RENAME VALUE 'EU' TO 'eu'")
    op.execute("ALTER TYPE checkregion RENAME VALUE 'ASIA' TO 'asia'")


def downgrade() -> None:
    op.execute("ALTER TYPE checkregion RENAME VALUE 'asia' TO 'ASIA'")
    op.execute("ALTER TYPE checkregion RENAME VALUE 'eu' TO 'EU'")
    op.execute("ALTER TYPE checkregion RENAME VALUE 'us' TO 'US'")

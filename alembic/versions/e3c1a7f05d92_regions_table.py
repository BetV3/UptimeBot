"""regions table: check regions become rows, not an enum (CP-1)

Revision ID: e3c1a7f05d92
Revises: d8f1a5c93b67
Create Date: 2026-10-20 00:00:00.000000

Before: Postgres enum ``checkregion`` (us/eu/asia) on checks, pending_checks
and worker_heartbeats, plus a hardcoded list in the scheduler. Adding a region
meant a migration. After: a ``regions`` table with two flags,

- scheduled: the scheduler enqueues work for it and its workers may claim jobs
- voting:    its latest fresh result counts toward a monitor's UP/DOWN verdict

and the three region columns become varchar FKs to ``regions.code``.

Seed keeps the three live regions exactly as they are (us, eu, asia: scheduled
and voting) so the running 1.0.0 workers keep working with no config change.
``us`` is NOT renamed: the old New York worker reports ``REGION=us`` and cannot
be reconfigured. The three US regions are added inactive (not scheduled, not
voting); each is switched on when its worker is up:

    update regions set scheduled = true where code = 'us-west';   -- shadow
    update regions set voting = true where code = 'us-west';      -- counts

Downgrade refuses (raises) if any row references a region outside us/eu/asia.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e3c1a7f05d92"
down_revision: Union[str, None] = "d8f1a5c93b67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("checks", "pending_checks", "worker_heartbeats")

SEED = [
    # code,        label,         city,            provider,       scheduled, voting
    ("us",         "US",          "New York",      "vultr",        True,  True),
    ("eu",         "EU",          "Frankfurt",     "vultr",        True,  True),
    ("asia",       "Asia",        "Singapore",     "vultr",        True,  True),
    ("us-east",    "US East",     "New York",      "vultr",        False, False),
    ("us-central", "US Central",  "Dallas",        "akamai",       False, False),
    ("us-west",    "US West",     "San Francisco", "digitalocean", False, False),
]


def upgrade() -> None:
    regions = op.create_table(
        "regions",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("city", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("scheduled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("voting", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("code ~ '^[a-z][a-z0-9-]{1,31}$'", name="ck_regions_code_format"),
        sa.CheckConstraint("scheduled OR NOT voting", name="ck_regions_voting_needs_scheduled"),
    )
    op.bulk_insert(
        regions,
        [dict(code=c, label=l, city=ci, provider=p, scheduled=s, voting=v) for c, l, ci, p, s, v in SEED],
    )
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ALTER COLUMN region TYPE varchar(32) USING region::text")
        op.create_foreign_key(f"fk_{t}_region", t, "regions", ["region"], ["code"], onupdate="CASCADE")
    op.execute("DROP TYPE checkregion")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM checks WHERE region NOT IN ('us','eu','asia'))
             OR EXISTS (SELECT 1 FROM pending_checks WHERE region NOT IN ('us','eu','asia'))
             OR EXISTS (SELECT 1 FROM worker_heartbeats WHERE region NOT IN ('us','eu','asia'))
          THEN
            RAISE EXCEPTION 'CP-1 downgrade: rows reference regions other than us/eu/asia; delete them first';
          END IF;
        END $$;
        """
    )
    op.execute("CREATE TYPE checkregion AS ENUM ('us', 'eu', 'asia')")
    for t in TABLES:
        op.drop_constraint(f"fk_{t}_region", t, type_="foreignkey")
        op.execute(f"ALTER TABLE {t} ALTER COLUMN region TYPE checkregion USING region::checkregion")
    op.drop_table("regions")

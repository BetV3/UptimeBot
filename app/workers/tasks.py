import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.models import Monitor, PendingCheck, CheckRegion
from app.workers.celery_app import celery_app

settings = get_settings()
sync_engine = create_engine(settings.database_url_sync)

REGIONS = [CheckRegion.US, CheckRegion.EU, CheckRegion.ASIA]


@celery_app.task(name="app.workers.tasks.schedule_pending_checks")
def schedule_pending_checks():
    """Find monitors due for a check and create PendingCheck rows for each region."""
    with Session(sync_engine) as session:
        now = datetime.now(timezone.utc)

        # Get active monitors that are due for a check.
        # A monitor is due when there are no pending (unclaimed) checks for it
        # and its last check is older than its interval (or it has never been checked).
        monitors = session.execute(
            select(Monitor).where(Monitor.is_active.is_(True))
        ).scalars().all()

        created = 0
        for monitor in monitors:
            # Skip if there are already unclaimed pending checks for this monitor
            existing = session.execute(
                select(func.count())
                .select_from(PendingCheck)
                .where(
                    PendingCheck.monitor_id == monitor.id,
                    PendingCheck.claimed_at.is_(None),
                )
            ).scalar()
            if existing > 0:
                continue

            # Check if enough time has passed since the last scheduled check
            last_scheduled = session.execute(
                select(PendingCheck.scheduled_at)
                .where(PendingCheck.monitor_id == monitor.id)
                .order_by(PendingCheck.scheduled_at.desc())
                .limit(1)
            ).scalar()

            if last_scheduled is not None:
                elapsed = (now - last_scheduled).total_seconds()
                if elapsed < monitor.interval_seconds:
                    continue

            # Create pending checks for each region
            for region in REGIONS:
                pc = PendingCheck(
                    id=uuid.uuid4(),
                    monitor_id=monitor.id,
                    region=region,
                    scheduled_at=now,
                )
                session.add(pc)
                created += 1

        session.commit()
        return {"created": created}

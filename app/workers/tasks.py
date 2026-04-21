import logging
import uuid
from datetime import datetime, timedelta, timezone

from redis import Redis
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.models import (
    AlertChannel,
    AlertDelivery,
    AlertDeliveryState,
    Check,
    CheckStatus,
    Incident,
    Monitor,
    MonitorStatus,
    PendingCheck,
    Project,
    CheckRegion,
)
from app.services.alerts import enqueue_alerts_sync, send_delivery_sync
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

settings = get_settings()
sync_engine = create_engine(settings.database_url_sync)

REGIONS = [CheckRegion.US, CheckRegion.EU, CheckRegion.ASIA]

# Mirror of MAX_ATTEMPTS in app/api/routes/internal.py. Kept in sync manually;
# if this drifts, a reaped row could be leased once more than intended.
MAX_ATTEMPTS = 3

# Beat heartbeat key + TTL. schedule_pending_checks writes on every run;
# /healthz/beat reads and compares against freshness threshold.
BEAT_HEARTBEAT_KEY = "checkpulse:beat:last_seen"
BEAT_HEARTBEAT_TTL_SECONDS = 300


@celery_app.task(name="app.workers.tasks.schedule_pending_checks")
def schedule_pending_checks():
    """Enqueue regional pending_checks for every monitor whose next_check_at has arrived.

    Advances next_check_at by interval_seconds so the monitor is not re-enqueued
    until the next interval boundary, independent of whether workers actually
    complete the check. Stuck/dead regional workers are handled by the lease
    system (internal/jobs re-claims expired leases; reap_dead_pending_checks
    retires exhausted-attempts rows).
    """
    with Session(sync_engine) as session:
        now = datetime.now(timezone.utc)

        due = session.execute(
            select(Monitor).where(
                Monitor.is_active.is_(True),
                Monitor.next_check_at.is_not(None),
                Monitor.next_check_at <= now,
            )
        ).scalars().all()

        created = 0
        for monitor in due:
            for region in REGIONS:
                # ON CONFLICT DO NOTHING against the unique partial index
                # (monitor_id, region) WHERE dead = false. If a live row
                # already exists for this (monitor, region), we skip — the
                # worker will eventually claim or the reaper will kill it.
                stmt = (
                    pg_insert(PendingCheck)
                    .values(
                        id=uuid.uuid4(),
                        monitor_id=monitor.id,
                        region=region,
                        scheduled_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["monitor_id", "region"],
                        index_where=text("dead = false"),
                    )
                )
                result = session.execute(stmt)
                if result.rowcount:
                    created += 1

            monitor.next_check_at = now + timedelta(seconds=monitor.interval_seconds)

        session.commit()

    # Beat liveness heartbeat. /healthz/beat alerts if this stops being
    # written — indicates celery-beat is stuck or the scheduler task is
    # broken. Writing AFTER the commit means a failing task doesn't
    # falsely report healthy.
    try:
        Redis.from_url(settings.redis_url).set(
            BEAT_HEARTBEAT_KEY,
            int(now.timestamp()),
            ex=BEAT_HEARTBEAT_TTL_SECONDS,
        )
    except Exception:
        logger.exception("failed to write beat heartbeat")

    return {"due": len(due), "created": created}


@celery_app.task(name="app.workers.tasks.reap_dead_pending_checks")
def reap_dead_pending_checks():
    """Mark pending_checks whose lease has expired and whose attempts reached the
    cap as dead. This frees the unique-index slot so the scheduler can enqueue
    a fresh row on the next interval boundary.

    Also marks orphaned rows (leased_at expired, no worker reclaim, under cap)
    after a large grace window so a permanently-dead region doesn't wedge the
    slot forever. Grace = 5× interval so a healthy region recovering from
    flapping has room to catch up naturally.
    """
    with Session(sync_engine) as session:
        # Cap-exhausted rows with expired leases → dead.
        result_exhausted = session.execute(
            update(PendingCheck)
            .where(
                PendingCheck.dead.is_(False),
                PendingCheck.lease_expires_at.is_not(None),
                PendingCheck.lease_expires_at < datetime.now(timezone.utc),
                PendingCheck.attempts >= MAX_ATTEMPTS,
            )
            .values(dead=True)
        )

        # Orphans: rows older than 10 minutes that were never even leased
        # (all workers for this region are gone) → dead. Without this, a dead
        # region permanently wedges the (monitor, region) slot.
        ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
        result_orphans = session.execute(
            update(PendingCheck)
            .where(
                PendingCheck.dead.is_(False),
                PendingCheck.leased_at.is_(None),
                PendingCheck.scheduled_at < ten_minutes_ago,
            )
            .values(dead=True)
        )

        session.commit()
        return {
            "reaped_exhausted": result_exhausted.rowcount or 0,
            "reaped_orphans": result_orphans.rowcount or 0,
        }


@celery_app.task(
    name="app.workers.tasks.send_alert_delivery",
    bind=True,
    autoretry_for=(Exception,),
    max_retries=4,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def send_alert_delivery(self, delivery_id: str):
    """Send one alert_delivery row via its channel. Idempotent: the row's
    terminal state (sent/failed) short-circuits a re-run so a duplicated
    enqueue is harmless.

    Raising lets Celery retry with exponential backoff. After max_retries we
    mark the row failed and swallow so the worker doesn't loop.
    """
    with Session(sync_engine) as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        if delivery is None:
            logger.warning(f"send_alert_delivery: {delivery_id} not found")
            return {"skipped": "not_found"}

        if delivery.state in (AlertDeliveryState.SENT.value, AlertDeliveryState.FAILED.value):
            return {"skipped": delivery.state}

        channel = session.get(AlertChannel, delivery.alert_channel_id)
        if channel is None or not channel.is_active:
            delivery.state = AlertDeliveryState.FAILED.value
            delivery.last_error = "Channel missing or inactive"
            session.commit()
            return {"skipped": "channel_gone"}

        incident = session.get(Incident, delivery.incident_id)
        if incident is None:
            delivery.state = AlertDeliveryState.FAILED.value
            delivery.last_error = "Incident missing"
            session.commit()
            return {"skipped": "incident_gone"}

        monitor = session.get(Monitor, incident.monitor_id)
        if monitor is None:
            delivery.state = AlertDeliveryState.FAILED.value
            delivery.last_error = "Monitor missing"
            session.commit()
            return {"skipped": "monitor_gone"}

        project = session.get(Project, monitor.project_id)
        project_name = project.name if project else "Unknown"

        delivery.attempts = (delivery.attempts or 0) + 1
        session.commit()

        try:
            send_delivery_sync(channel, monitor, incident, delivery.kind, project_name)
        except Exception as e:
            delivery.last_error = str(e)[:1000]
            if self.request.retries >= self.max_retries:
                delivery.state = AlertDeliveryState.FAILED.value
                session.commit()
                logger.exception(f"send_alert_delivery {delivery_id} exhausted retries")
                return {"failed": str(e)[:200]}
            session.commit()
            raise

        delivery.state = AlertDeliveryState.SENT.value
        delivery.sent_at = datetime.now(timezone.utc)
        delivery.last_error = None
        session.commit()
        return {"sent": delivery_id}


@celery_app.task(
    name="app.workers.tasks.finalize_check_result",
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def finalize_check_result(monitor_id: str):
    """Recompute consensus status for one monitor, manage the incident
    lifecycle, and enqueue alert_deliveries rows on transitions.

    Serialized per-monitor by a transaction-scoped advisory lock so two
    regions posting near-simultaneously can't both drive the consensus
    flip and double-write an incident. The lock is released automatically
    when the transaction commits/rolls back.

    Alert dispatches are fired after commit — otherwise the alerts worker
    could race ahead and look up an uncommitted delivery.
    """
    mid = uuid.UUID(monitor_id)
    new_delivery_ids: list[str] = []

    with Session(sync_engine) as session:
        # pg_advisory_xact_lock takes a bigint. hashtext(uuid::text) maps
        # the monitor_id into that space; collisions are possible but
        # harmless — the worst case is two unrelated monitors serializing
        # against each other for the duration of one finalize call.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:mid))"),
            {"mid": str(mid)},
        )

        monitor = session.get(Monitor, mid)
        if monitor is None:
            session.commit()
            return {"skipped": "monitor_missing"}

        old_status = monitor.current_status

        down_count = 0
        total_checked = 0
        for region in REGIONS:
            latest = session.execute(
                select(Check.status)
                .where(Check.monitor_id == mid, Check.region == region)
                .order_by(Check.checked_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest is not None:
                total_checked += 1
                if latest == CheckStatus.DOWN:
                    down_count += 1

        if total_checked == 0:
            session.commit()
            return {"skipped": "no_checks"}

        new_status = MonitorStatus.DOWN if down_count >= 2 else MonitorStatus.UP
        monitor.current_status = new_status

        now = datetime.now(timezone.utc)

        if old_status != MonitorStatus.DOWN and new_status == MonitorStatus.DOWN:
            incident = Incident(monitor_id=monitor.id, started_at=now)
            session.add(incident)
            session.flush()
            new_delivery_ids = enqueue_alerts_sync(session, monitor, incident, "down")

        elif old_status == MonitorStatus.DOWN and new_status == MonitorStatus.UP:
            incident = session.execute(
                select(Incident)
                .where(
                    Incident.monitor_id == monitor.id,
                    Incident.resolved_at.is_(None),
                )
                .order_by(Incident.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if incident:
                incident.resolved_at = now
                session.flush()
                new_delivery_ids = enqueue_alerts_sync(session, monitor, incident, "resolved")

        session.commit()

    for did in new_delivery_ids:
        send_alert_delivery.delay(did)

    return {
        "monitor_id": monitor_id,
        "old_status": old_status.value if old_status else None,
        "new_status": new_status.value,
        "deliveries": len(new_delivery_ids),
    }


@celery_app.task(name="app.workers.tasks.cleanup_old_checks")
def cleanup_old_checks():
    """Delete check records older than 90 days to manage storage."""
    with Session(sync_engine) as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        result = session.execute(
            text("DELETE FROM checks WHERE checked_at < :cutoff"),
            {"cutoff": cutoff},
        )
        deleted = result.rowcount
        session.commit()
        return {"deleted": deleted}

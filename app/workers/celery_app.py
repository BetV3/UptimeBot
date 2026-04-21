from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "uptimebot",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_default_queue="default",
    task_queues=(
        Queue("default"),
        Queue("alerts"),
    ),
    # send_alert_delivery runs on its own worker so a slow notification
    # provider can't starve the scheduler/reaper on the default queue.
    task_routes={
        "app.workers.tasks.send_alert_delivery": {"queue": "alerts"},
    },
    beat_schedule={
        "schedule-checks": {
            "task": "app.workers.tasks.schedule_pending_checks",
            "schedule": 15.0,
        },
        "reap-dead-pending-checks": {
            "task": "app.workers.tasks.reap_dead_pending_checks",
            "schedule": 60.0,
        },
        "cleanup-old-checks": {
            "task": "app.workers.tasks.cleanup_old_checks",
            "schedule": crontab(hour=3, minute=0),  # Daily at 3:00 AM UTC
        },
    },
)

from celery import Celery
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
    beat_schedule={
        "schedule-checks": {
            "task": "app.workers.tasks.schedule_pending_checks",
            "schedule": 15.0,
        },
    },
)

from celery import Celery
from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "uptimebot",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        # Will be added in Week 3:
        # "schedule-checks": {
        #     "task": "app.workers.tasks.schedule_pending_checks",
        #     "schedule": 15.0,  # every 15 seconds
        # },
    },
)

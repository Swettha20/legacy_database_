import os
from celery import Celery

celery = Celery("moderniser", broker=os.getenv("REDIS_URL", "redis://queue:6379/0"))
celery.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json",
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True, task_ignore_result=True,
    broker_transport_options={"visibility_timeout": 86400})

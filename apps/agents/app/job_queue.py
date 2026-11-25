import hashlib
import os
from typing import Any

import redis
from rq import Queue, Retry
from rq.exceptions import DuplicateJobError, NoSuchJobError
from rq.job import Job
from rq.registry import FailedJobRegistry

from .database import ReviewDatabase

QUEUE_NAME = "reviewpilot-webhooks"
SUCCESS_RETENTION_SECONDS = 30 * 24 * 60 * 60
FAILURE_RETENTION_SECONDS = 90 * 24 * 60 * 60
MAX_JOB_RUNTIME_SECONDS = 10 * 60


class QueueUnavailableError(RuntimeError):
    pass


def redis_connection() -> redis.Redis:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise QueueUnavailableError("REDIS_URL is required for durable webhook processing.")
    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def webhook_job_id(event: dict[str, Any]) -> str:
    identity = (
        f"{event['owner'].lower()}/{event['repo'].lower()}:"
        f"{event['pull_number']}:{event['head_sha']}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"webhook-{digest}"


def enqueue_webhook_review(event: dict[str, Any]) -> tuple[str, bool]:
    from .tasks import process_webhook_review_job

    job_id = webhook_job_id(event)
    database = ReviewDatabase()
    database.register_webhook_job(job_id, event)
    persisted_job = database.get_job(job_id)
    if persisted_job and persisted_job["status"] == "completed":
        return job_id, False

    try:
        connection = redis_connection()
        queue = Queue(QUEUE_NAME, connection=connection)
        try:
            existing_job = Job.fetch(job_id, connection=connection)
        except NoSuchJobError:
            existing_job = None

        if existing_job is not None:
            status = existing_job.get_status(refresh=True)
            status_value = status.value if hasattr(status, "value") else str(status)
            if status_value == "failed":
                FailedJobRegistry(queue=queue).remove(job_id, delete_job=True)
            else:
                return job_id, False

        queue.enqueue(
            process_webhook_review_job,
            job_id,
            event,
            job_id=job_id,
            unique=True,
            retry=Retry(max=3, interval=[15, 60, 180]),
            job_timeout=MAX_JOB_RUNTIME_SECONDS,
            result_ttl=SUCCESS_RETENTION_SECONDS,
            failure_ttl=FAILURE_RETENTION_SECONDS,
            description=(
                f"Review {event['owner']}/{event['repo']} "
                f"PR #{event['pull_number']} at {event['head_sha']}"
            ),
        )
        database.update_job(job_id, "queued")
    except DuplicateJobError:
        return job_id, False
    except redis.RedisError as exc:
        database.update_job(job_id, "queue_failed", error=str(exc))
        raise QueueUnavailableError("Could not access the webhook job queue.") from exc

    return job_id, True


def queued_job_status(job_id: str) -> str | None:
    try:
        connection = redis_connection()
        job = Job.fetch(job_id, connection=connection)
    except redis.RedisError as exc:
        raise QueueUnavailableError("Could not access the webhook job queue.") from exc
    except NoSuchJobError:
        return None
    status = job.get_status(refresh=True)
    return status.value if hasattr(status, "value") else str(status)

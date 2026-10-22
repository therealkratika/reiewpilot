import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from redis.exceptions import RedisError
from rq import get_current_job

from .database import ReviewDatabase
from .webhook import (
    RedisWebhookEventStore,
    WebhookLeaseLostError,
    event_key,
    initiate_review,
)

logger = logging.getLogger("reviewpilot.jobs")
LEASE_TTL_SECONDS = 180
LEASE_REFRESH_SECONDS = 45


def process_webhook_review_job(job_id: str, event: dict[str, Any]) -> dict[str, Any]:
    database = ReviewDatabase()
    database.update_job(job_id, "started", increment_attempts=True)
    key = event_key(event)
    store = None
    token = None
    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()
    audit_recorded = False

    def heartbeat() -> None:
        while not stop_heartbeat.wait(LEASE_REFRESH_SECONDS):
            if store is None or token is None:
                return
            try:
                if not store.refresh(key, token):
                    lease_lost.set()
                    logger.warning("Webhook review lease lost for job %s", job_id)
                    return
            except RedisError:
                lease_lost.set()
                logger.exception("Could not refresh webhook review lease for job %s", job_id)
                return

    def assert_lease_owned() -> None:
        if (
            lease_lost.is_set()
            or store is None
            or token is None
            or not store.owns(key, token)
        ):
            lease_lost.set()
            raise WebhookLeaseLostError("Webhook review lease is no longer owned.")

    def before_publish() -> None:
        nonlocal audit_recorded
        assert_lease_owned()
        if not audit_recorded:
            database.record_audit_event(
                user_id=None,
                action="webhook.review.publish",
                owner=event["owner"],
                repo=event["repo"],
                pull_number=event["pull_number"],
                resource_id=event["head_sha"],
                outcome="requested",
                created_at=datetime.now(timezone.utc),
            )
            audit_recorded = True

    heartbeat_thread = None
    try:
        store = RedisWebhookEventStore(
            redis_url=os.getenv("REDIS_URL"),
            ttl_seconds=LEASE_TTL_SECONDS,
        )
        token = store.claim(key)
        if token is None:
            raise RuntimeError("A different worker currently owns this webhook review lease.")
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        result = initiate_review(event, before_publish=before_publish)
        assert_lease_owned()
        database.save_review_result(event, result, job_id=job_id)
        database.update_job(job_id, "completed")
        logger.info(
            "Webhook job %s completed for %s PR #%s at %s with status %s",
            job_id,
            f"{event['owner']}/{event['repo']}",
            event["pull_number"],
            event["head_sha"],
            result.get("status", "unknown"),
        )
        return {"status": result.get("status", "completed")}
    except Exception as exc:
        current_job = get_current_job()
        retrying = bool(current_job and current_job.retries_left)
        try:
            database.update_job(
                job_id,
                "retrying" if retrying else "failed",
                error=str(exc)[:2000],
            )
        except Exception:
            logger.exception("Could not persist failure status for webhook job %s", job_id)
        logger.exception(
            "Webhook job %s failed for %s PR #%s",
            job_id,
            f"{event['owner']}/{event['repo']}",
            event["pull_number"],
        )
        raise
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=LEASE_REFRESH_SECONDS + 1)
        if store is not None and token is not None:
            try:
                store.release(key, token)
            except RedisError:
                logger.exception("Could not release webhook lease for job %s", job_id)

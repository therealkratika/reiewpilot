import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from rq.exceptions import DuplicateJobError, NoSuchJobError
from redis.exceptions import ConnectionError as RedisConnectionError

from app import job_queue
from app.database import ReviewDatabase


EVENT = {
    "owner": "acme",
    "repo": "widgets",
    "pull_number": 7,
    "head_sha": "a" * 40,
    "action": "synchronize",
}


class FakeJob:
    jobs = {}
    fetch_barrier = None

    def __init__(self, job_id):
        self.id = job_id

    @classmethod
    def fetch(cls, job_id, connection=None):
        if cls.fetch_barrier is not None:
            cls.fetch_barrier.wait(timeout=5)
        if job_id not in cls.jobs:
            raise NoSuchJobError(job_id)
        return cls(job_id)

    def get_status(self, refresh=False):
        return self.jobs[self.id]


class FakeFailedRegistry:
    def __init__(self, queue):
        self.queue = queue

    def remove(self, job_id, delete_job=False):
        assert delete_job is True
        self.queue.jobs.pop(job_id, None)


class FakeQueue:
    def __init__(self):
        self.jobs = FakeJob.jobs
        self.lock = threading.Lock()
        self.enqueued = []

    def enqueue(self, function, *args, **kwargs):
        job_id = kwargs["job_id"]
        with self.lock:
            if job_id in self.jobs:
                raise DuplicateJobError()
            self.jobs[job_id] = "queued"
            self.enqueued.append((function, args, kwargs))


def _configure_fake_queue(monkeypatch, tmp_path):
    FakeJob.jobs = {}
    FakeJob.fetch_barrier = None
    queue = FakeQueue()
    monkeypatch.setattr(job_queue, "ReviewDatabase", lambda: ReviewDatabase(tmp_path / "jobs.db"))
    monkeypatch.setattr(job_queue, "redis_connection", lambda: object())
    monkeypatch.setattr(job_queue, "Queue", lambda name, connection: queue)
    monkeypatch.setattr(job_queue, "Job", FakeJob)
    monkeypatch.setattr(job_queue, "FailedJobRegistry", FakeFailedRegistry)
    return queue


def test_webhook_job_id_is_stable_and_repository_case_insensitive():
    lower = dict(EVENT, owner="acme", repo="widgets")
    upper = dict(EVENT, owner="Acme", repo="Widgets")

    assert job_queue.webhook_job_id(lower) == job_queue.webhook_job_id(upper)


def test_enqueue_uses_unique_rq_jobs_and_retries(monkeypatch, tmp_path):
    queue = _configure_fake_queue(monkeypatch, tmp_path)

    job_id, enqueued = job_queue.enqueue_webhook_review(EVENT)

    assert enqueued is True
    assert queue.enqueued[0][2]["unique"] is True
    assert queue.enqueued[0][2]["retry"].max == 3
    assert queue.enqueued[0][2]["retry"].intervals == [15, 60, 180]
    assert job_queue.enqueue_webhook_review(EVENT) == (job_id, False)


def test_concurrent_deliveries_create_only_one_job(monkeypatch, tmp_path):
    queue = _configure_fake_queue(monkeypatch, tmp_path)
    FakeJob.fetch_barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: job_queue.enqueue_webhook_review(EVENT), range(2)))

    assert sorted(enqueued for _, enqueued in results) == [False, True]
    assert len(queue.enqueued) == 1


def test_failed_rq_job_can_be_reenqueued_on_github_redelivery(monkeypatch, tmp_path):
    queue = _configure_fake_queue(monkeypatch, tmp_path)
    job_id = job_queue.webhook_job_id(EVENT)
    FakeJob.jobs[job_id] = "failed"

    returned_id, enqueued = job_queue.enqueue_webhook_review(EVENT)

    assert returned_id == job_id
    assert enqueued is True
    assert FakeJob.jobs[job_id] == "queued"


def test_job_status_surfaces_redis_outage(monkeypatch):
    monkeypatch.setattr(job_queue, "redis_connection", lambda: object())

    def fail_fetch(*args, **kwargs):
        raise RedisConnectionError("offline")

    monkeypatch.setattr(job_queue.Job, "fetch", fail_fetch)

    with pytest.raises(job_queue.QueueUnavailableError) as raised:
        job_queue.queued_job_status("missing")
    assert isinstance(raised.value.__cause__, RedisConnectionError)

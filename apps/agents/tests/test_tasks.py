import time
from types import SimpleNamespace

import pytest

from app import tasks


EVENT = {
    "owner": "acme",
    "repo": "widgets",
    "pull_number": 7,
    "head_sha": "a" * 40,
    "action": "synchronize",
}


class FakeDatabase:
    def __init__(self):
        self.updates = []
        self.saved = []

    def update_job(self, job_id, status, **kwargs):
        self.updates.append((job_id, status, kwargs))

    def save_review_result(self, event, result, *, job_id):
        self.saved.append((event, result, job_id))


class FakeLeaseStore:
    current_token = None
    refreshed = 0
    released = []
    next_token = 0

    def __init__(self, **kwargs):
        pass

    def claim(self, key):
        if self.current_token is not None:
            return None
        type(self).next_token += 1
        self.current_token = str(self.next_token)
        return self.current_token

    def owns(self, key, token):
        return self.current_token == token

    def refresh(self, key, token):
        if not self.owns(key, token):
            return False
        type(self).refreshed += 1
        return True

    def release(self, key, token):
        if not self.owns(key, token):
            return False
        self.current_token = None
        type(self).released.append(token)
        return True


def _configure_worker(monkeypatch):
    database = FakeDatabase()
    FakeLeaseStore.current_token = None
    FakeLeaseStore.refreshed = 0
    FakeLeaseStore.released = []
    monkeypatch.setattr(tasks, "ReviewDatabase", lambda: database)
    monkeypatch.setattr(tasks, "RedisWebhookEventStore", FakeLeaseStore)
    monkeypatch.setattr(tasks, "LEASE_REFRESH_SECONDS", 0.005)
    monkeypatch.setattr(tasks, "get_current_job", lambda: SimpleNamespace(retries_left=1))
    return database


def test_worker_renews_lease_during_long_review(monkeypatch):
    database = _configure_worker(monkeypatch)

    def review(event, before_publish):
        before_publish()
        time.sleep(0.03)
        return {"status": "no_findings"}

    monkeypatch.setattr(tasks, "initiate_review", review)

    result = tasks.process_webhook_review_job("job-1", EVENT)

    assert result == {"status": "no_findings"}
    assert FakeLeaseStore.refreshed >= 1
    assert len(FakeLeaseStore.released) == 1
    assert database.updates[-1][1] == "completed"


def test_failed_worker_releases_lease_for_rq_retry(monkeypatch):
    database = _configure_worker(monkeypatch)
    attempts = 0

    def review(event, before_publish):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary provider failure")
        before_publish()
        return {"status": "no_findings"}

    monkeypatch.setattr(tasks, "initiate_review", review)

    with pytest.raises(RuntimeError, match="temporary provider failure"):
        tasks.process_webhook_review_job("job-1", EVENT)

    assert database.updates[-1][1] == "retrying"
    assert FakeLeaseStore.current_token is None

    result = tasks.process_webhook_review_job("job-1", EVENT)

    assert result == {"status": "no_findings"}
    assert attempts == 2
    assert database.updates[-1][1] == "completed"
    assert FakeLeaseStore.current_token is None

"""Idempotent job queue.

Jobs live in a Redis set, so re-enqueueing the same run id is a no-op and a
restarted worker cannot create duplicate work by replaying the queue.  Popping
is destructive (SPOP); durability comes from database truth: ``worker.recover``
re-adds every study still in ``queued``/``running`` state at startup.
"""
from redis import Redis

from ..config.settings import REDIS_URL

queue = Redis.from_url(REDIS_URL, decode_responses=True)
JOB_KEY = "spectral:jobs"


def enqueue(run_id):
    queue.sadd(JOB_KEY, run_id)


def dequeue(timeout=2):
    """Return ``(key, id)`` for one pending job, or ``None``.

    The signature mirrors the previous list-based BLPOP call sites.  ``timeout``
    is accepted for compatibility; polling waits briefly when the set is empty.
    """
    value = queue.spop(JOB_KEY)
    if value is not None:
        return (JOB_KEY, value)
    if timeout:
        import time

        time.sleep(min(timeout, 0.1))
    return None


def pending():
    return queue.smembers(JOB_KEY)

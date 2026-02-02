from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from .config import QUEUE_NAME, REDIS_URL


if __name__ == "__main__":
    redis_conn = Redis.from_url(REDIS_URL)
    queue = Queue(QUEUE_NAME, connection=redis_conn)
    worker = Worker([queue], connection=redis_conn)
    worker.work(with_scheduler=True)

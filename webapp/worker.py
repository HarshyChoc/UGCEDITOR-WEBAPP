from __future__ import annotations

import time

from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue, Worker

from .config import QUEUE_NAME, REDIS_URL


def create_redis_connection(url: str, max_retries: int = 5, retry_delay: float = 2.0) -> Redis:
    """Create Redis connection with retry logic."""
    last_error = None
    for attempt in range(max_retries):
        try:
            conn = Redis.from_url(url)
            conn.ping()  # Test connection
            return conn
        except RedisConnectionError as e:
            last_error = e
            if attempt < max_retries - 1:
                print(f"Redis connection attempt {attempt + 1} failed, retrying...")
                time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
    raise RedisConnectionError(f"Failed to connect to Redis after {max_retries} attempts: {last_error}")


if __name__ == "__main__":
    redis_conn = create_redis_connection(REDIS_URL)
    queue = Queue(QUEUE_NAME, connection=redis_conn)
    worker = Worker([queue], connection=redis_conn)
    worker.work(with_scheduler=True)

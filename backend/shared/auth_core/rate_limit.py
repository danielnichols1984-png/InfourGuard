import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_attempts: dict[str, deque] = defaultdict(deque)


def enforce_rate_limit(
    request: Request, key_prefix: str, limit: int, window_seconds: int
) -> None:
    """Fixed-window rate limit per client IP, in-process memory only.

    Good enough for a single-instance deployment. Behind multiple workers
    or processes, each one tracks its own counts, so the effective limit
    is (limit * worker count) — swap this for a shared store (e.g. Redis)
    if that matters for your deployment.
    """
    client_ip = request.client.host if request.client else "unknown"
    key = f"{key_prefix}:{client_ip}"
    now = time.monotonic()

    bucket = _attempts[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()

    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")

    bucket.append(now)

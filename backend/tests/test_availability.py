"""Availability: does bad input, a missing resource, or one user's activity
degrade service for anyone else?

Note on scope: this suite deliberately does NOT try to saturate the app's
40-thread background pool (documented in shared/migrations_core/README.md
and discussed with the operator) with many real concurrent migrations —
doing that for real would mean hammering live Dropbox/Google APIs many
times over just to prove a limit that's already evident from the code
itself. What's tested here is real: clean error handling under bad input,
and that ordinary concurrent traffic doesn't contend with itself.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from conftest import BASE_URL


def test_rate_limiter_blocks_once_its_limit_is_reached():
    """Exercises the real production rate-limiting function directly with
    its own isolated key, rather than firing many real /auth/login
    requests — those all share one IP-keyed bucket across this whole
    suite's fixtures (each of which logs in), so testing it that way would
    either get contaminated by other tests or contaminate them."""
    import pytest as _pytest
    from fastapi import HTTPException
    from shared.auth_core.rate_limit import enforce_rate_limit

    class FakeClient:
        host = "203.0.113.99"  # TEST-NET-3 — not a real address

    class FakeRequest:
        client = FakeClient()

    request = FakeRequest()
    key = f"availability-test-{time.time()}"  # unique per run, no cross-test bleed

    for _ in range(5):
        enforce_rate_limit(request, key, limit=5, window_seconds=60)

    with _pytest.raises(HTTPException) as exc_info:
        enforce_rate_limit(request, key, limit=5, window_seconds=60)
    assert exc_info.value.status_code == 429


def test_nonexistent_job_returns_clean_404_or_403_not_500(user_a):
    r = user_a.get("/migrations/jobs/999999999")
    assert r.status_code in (403, 404)


def test_malformed_json_body_returns_clean_4xx_not_500(user_a):
    r = user_a.session.post(
        f"{BASE_URL}/migrations/jobs",
        data="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert 400 <= r.status_code < 500


def test_unknown_provider_name_is_rejected_cleanly(user_a):
    r = user_a.post(
        "/migrations/jobs",
        json={"source_provider": "onedrive", "destination_provider": "google"},
    )
    assert r.status_code == 400


def test_browser_style_404_returns_html_not_json(anon):
    r = anon.session.get(f"{BASE_URL}/this-route-does-not-exist", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers.get("content-type", "")


def test_fetch_style_404_still_returns_json(anon):
    """The polarity that matters most: a bare fetch()-style request (no
    explicit Accept) must NOT get an HTML page, or every JS-driven page's
    error handling in this app would silently break."""
    r = anon.session.get(f"{BASE_URL}/migrations/jobs/999999999", headers={"Accept": "*/*"})
    assert "application/json" in r.headers.get("content-type", "")


def test_concurrent_requests_from_multiple_users_all_succeed_promptly(user_a, user_b, admin_user):
    sessions = [user_a, user_b, admin_user]
    paths = ["/auth/me", "/subscriptions/me", "/migrations/jobs"] * 3

    def hit(i):
        session = sessions[i % len(sessions)]
        path = paths[i % len(paths)]
        return session.get(path).status_code

    start = time.time()
    with ThreadPoolExecutor(max_workers=15) as pool:
        results = list(pool.map(hit, range(15)))
    elapsed = time.time() - start

    assert all(code == 200 for code in results), results
    assert elapsed < 5, f"15 concurrent ordinary requests took {elapsed:.1f}s — investigate contention"

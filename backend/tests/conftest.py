"""Shared fixtures for the integration test suite.

These tests hit the *real* running server (http://localhost:8000) over
HTTP, using disposable synthetic accounts created per-test — never the
operator's real connected Dropbox/Google accounts, except where a test
explicitly reads (never writes) already-existing real job data for
accuracy/integrity spot-checks, skipping cleanly if that data isn't present
in a given environment.

Run from backend/, with the venv active and the server already running:
    pytest tests/ -v -s
"""
import sys
import uuid
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_URL = "http://localhost:8000"
TEST_PASSWORD = "testpassword123"


def _fresh_email() -> str:
    return f"citest_{uuid.uuid4().hex[:12]}@example.com"


class ApiSession:
    """A thin wrapper around a requests.Session tied to one logged-in
    (or anonymous) test user, with the base URL baked in."""

    def __init__(self):
        self.session = requests.Session()
        self.email: str | None = None
        self.user_id: int | None = None

    def signup_and_login(self) -> dict:
        email = _fresh_email()
        r = self.session.post(
            f"{BASE_URL}/auth/signup",
            data={"email": email, "password": TEST_PASSWORD},
        )
        assert r.status_code in (200, 302), f"signup failed: {r.status_code} {r.text}"

        r = self.session.post(
            f"{BASE_URL}/auth/login",
            data={"email": email, "password": TEST_PASSWORD},
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"

        me = r.json()
        self.email = email
        self.user_id = me["id"]
        return me

    def make_admin(self) -> None:
        """Promotes this already-logged-in user to admin directly via the
        DB, mirroring what the real `make_admin` CLI does (there's
        deliberately no API for this)."""
        from shared.auth_core.db import SessionLocal
        from shared.auth_core.models import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == self.user_id).first()
            user.is_admin = True
            db.commit()
        finally:
            db.close()

    def get(self, path, **kwargs):
        return self.session.get(f"{BASE_URL}{path}", **kwargs)

    def post(self, path, **kwargs):
        return self.session.post(f"{BASE_URL}{path}", **kwargs)

    def put(self, path, **kwargs):
        return self.session.put(f"{BASE_URL}{path}", **kwargs)

    def delete(self, path, **kwargs):
        return self.session.delete(f"{BASE_URL}{path}", **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _require_server_running():
    try:
        requests.get(BASE_URL, timeout=2)
    except requests.exceptions.ConnectionError:
        pytest.exit(f"Server not running at {BASE_URL} — start it before running this suite.")


@pytest.fixture
def user_a():
    s = ApiSession()
    s.signup_and_login()
    return s


@pytest.fixture
def user_b():
    s = ApiSession()
    s.signup_and_login()
    return s


@pytest.fixture
def admin_user():
    s = ApiSession()
    s.signup_and_login()
    s.make_admin()
    return s


@pytest.fixture
def anon():
    return ApiSession()

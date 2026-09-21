"""Confidentiality: can anyone see or touch data that isn't theirs?"""


def test_unauthenticated_requests_are_rejected(anon):
    assert anon.get("/migrations/jobs").status_code == 401
    assert anon.get("/auth/me").status_code == 401
    assert anon.get("/subscriptions/me").status_code == 401
    assert anon.get("/connect/status").status_code == 401


def test_user_cannot_see_another_users_job_in_their_list(user_a, user_b):
    r = user_a.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    )
    assert r.status_code == 200
    job_id = r.json()["id"]

    r = user_b.get("/migrations/jobs")
    assert r.status_code == 200
    assert job_id not in [j["id"] for j in r.json()]


def test_user_cannot_fetch_another_users_job_directly(user_a, user_b):
    job_id = user_a.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    ).json()["id"]

    r = user_b.get(f"/migrations/jobs/{job_id}")
    assert r.status_code == 403


def test_user_cannot_delete_another_users_job(user_a, user_b):
    job_id = user_a.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    ).json()["id"]

    r = user_b.delete(f"/migrations/jobs/{job_id}")
    assert r.status_code == 403

    # still there for the real owner
    assert user_a.get(f"/migrations/jobs/{job_id}").status_code == 200


def test_non_admin_cannot_create_mapping_for_someone_else(user_a, user_b):
    job_id = user_a.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    ).json()["id"]

    r = user_a.post(
        f"/migrations/jobs/{job_id}/mappings",
        json={
            "source_user_id": user_b.user_id,
            "destination_user_id": user_b.user_id,
            "source_root_path": "",
            "destination_root_path": "",
        },
    )
    assert r.status_code == 403


def test_admin_bypasses_the_self_only_mapping_restriction(admin_user, user_a):
    """Admin should get *past* the ownership check — and then fail for the
    expected next reason (user_a has no real provider connected), proving
    the admin bypass itself works rather than accidentally always denying."""
    job_id = admin_user.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    ).json()["id"]

    r = admin_user.post(
        f"/migrations/jobs/{job_id}/mappings",
        json={
            "source_user_id": user_a.user_id,
            "destination_user_id": user_a.user_id,
            "source_root_path": "",
            "destination_root_path": "",
        },
    )
    assert r.status_code == 400
    assert "connected" in r.json()["detail"].lower()


def test_non_admin_cannot_reach_subscription_admin_endpoints(user_a):
    assert user_a.get("/subscriptions/admin/users").status_code == 403


def test_non_admin_cannot_reach_admin_only_plan_mutations(user_a):
    r = user_a.post(
        "/subscriptions/admin/plans",
        json={"name": "sneaky", "display_name": "Sneaky", "price_display": "$0/mo", "features": []},
    )
    assert r.status_code == 403


def test_auth_me_never_leaks_password_fields(user_a):
    body = user_a.get("/auth/me").json()
    assert "password" not in body
    assert "hashed_password" not in body


def test_job_response_shape_has_no_extra_internal_fields(user_a):
    body = user_a.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    ).json()
    assert set(body.keys()) == {
        "id",
        "created_by_user_id",
        "source_provider",
        "destination_provider",
        "status",
        "notify_email",
        "scheduled_at",
    }


def test_mapping_response_never_includes_provider_tokens(admin_user):
    """Belt-and-suspenders: even though the schema shouldn't expose these,
    confirm no raw response body anywhere in the mapping-add flow leaks an
    access/refresh token string."""
    job_id = admin_user.post(
        "/migrations/jobs",
        json={"source_provider": "dropbox", "destination_provider": "google"},
    ).json()["id"]
    r = admin_user.post(
        f"/migrations/jobs/{job_id}/mappings",
        json={
            "source_user_id": admin_user.user_id,
            "destination_user_id": admin_user.user_id,
            "source_root_path": "",
            "destination_root_path": "",
        },
    )
    assert "access_token" not in r.text
    assert "refresh_token" not in r.text

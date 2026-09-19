# subscriptions_core

Plan tracking for a SaaS app: a `plans` table (Free/Pro/Team, editable) and
which plan each user is on. **No payment processing** — switching plans is
free and immediate. This is meant to let the data model and UI exist now,
with a real processor (Stripe, etc.) wired in later without reshaping
anything: `price_display` is just a string today; a real integration would
add `stripe_price_id`/`stripe_customer_id` columns and a webhook handler
alongside what's here.

## Dependency on auth_core

Unlike `auth_core`, this module is **not** fully standalone: its routes use
`shared.auth_core.dependencies.get_current_user`/`require_admin` to know who's
asking and whether they're an admin, and the admin user-listing endpoint
queries `shared.auth_core.models.User` directly. Copy `auth_core` alongside
it (or swap those imports for your own equivalents — a "current user"
dependency returning an object with `.id`/`.email`, and an "is admin" guard).

## Using it in a new app

1. Copy `shared/subscriptions_core/` (and `shared/auth_core/`, if not
   already present) into the new project.
2. Add to `.env`:
   ```
   SUBSCRIPTIONS_DATABASE_URL=postgresql://user:pass@host:port/dbname
   ```
3. In the app's entrypoint:
   ```python
   from shared.subscriptions_core.db import init_db, get_db
   from shared.subscriptions_core.routes import router as subscriptions_router
   from shared.subscriptions_core.service import seed_default_plans

   app.include_router(subscriptions_router, prefix="/subscriptions")

   @app.on_event("startup")
   def _init_subscriptions_db():
       init_db()
       db = next(get_db())
       try:
           seed_default_plans(db)
       finally:
           db.close()
   ```
4. Edit `SEED_PLANS` in `service.py` to match your actual product's plans
   before first running against a fresh database — it only ever seeds an
   empty `plans` table, so changes after that need a manual update/migration.

## Endpoints

- `GET /subscriptions/plans` — public, lists all plans.
- `GET /subscriptions/me` — current user's plan (defaults to whichever plan
  has `is_default=True` if they haven't chosen one).
- `POST /subscriptions/me` — `{"plan_id": <id>}`, switches the current
  user's plan immediately, no payment step.

Admin-only (require `auth_core`'s `require_admin`, i.e. `User.is_admin`):

- `POST /subscriptions/admin/plans` — create a plan.
- `PUT /subscriptions/admin/plans/{plan_id}` — update a plan's fields
  (partial update; only send what's changing). Setting `is_default: true`
  automatically clears the flag on every other plan.
- `DELETE /subscriptions/admin/plans/{plan_id}` — delete a plan; refuses
  (400) if any user is currently on it.
- `GET /subscriptions/admin/users` — every user with their current plan.
- `PUT /subscriptions/admin/users/{user_id}/plan` — `{"plan_id": <id>}`,
  force-sets a specific user's plan (comping, manual downgrades, etc).

There's no endpoint to grant admin rights — see `auth_core`'s README for
`make_admin`.

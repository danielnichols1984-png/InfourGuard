# businesses_core

Multi-user "business account" support: a `Business` can have several
platform users under it, one of them a `business_admin` who can add/remove
members, reset their passwords, view-as them, and email their usage
reports — a scoped-down clone of what a platform admin can do, restricted
to that business's own members. Individual (non-business) users have no
visibility into any business's data; every business-scoped endpoint only
ever queries rows matching the caller's own `business_id`.

## Dependency on auth_core and integrations_core

Like `subscriptions_core`, this module is **not** standalone: it uses
`auth_core`'s `get_current_user`/`require_admin` and calls
`auth_core.service.create_user`/`set_user_password` directly (member
accounts are real `auth_core` users). It also calls `integrations_core`'s
`generate_report`/`send_report_email` functions directly, with the target
member's user id, to support "email this member's report" without
impersonating them first. Copy `auth_core` and `integrations_core`
alongside this module if reusing it elsewhere.

## Using it in a new app

1. Copy `shared/businesses_core/` (plus `shared/auth_core/` and
   `shared/integrations_core/`) into the new project.
2. Add to `.env`:
   ```
   BUSINESSES_DATABASE_URL=postgresql://user:pass@host:port/dbname
   ```
3. In the app's entrypoint:
   ```python
   from shared.businesses_core.db import init_db as init_businesses_db
   from shared.businesses_core.routes import router as businesses_router

   app.include_router(businesses_router, prefix="/businesses")

   @app.on_event("startup")
   def _init_businesses_db():
       init_businesses_db()
   ```

## Endpoints (mounted under `/businesses`)

Platform admin (`auth_core`'s `require_admin`):

- `GET /admin/businesses` — every business with its member count.
- `POST /admin/businesses` — `{"name": ..., "admin_email": ...}` creates a
  business and makes that existing user its `business_admin`. 404s if the
  email isn't a registered user; 400s if that user is already in a
  business (a user belongs to at most one).
- `GET /admin/businesses/{id}/members` — oversight view of one business.
- `DELETE /admin/businesses/{id}` — 400s if it still has members.

Any business member:

- `GET /my-business` — business name + full member list with roles. 404s
  for anyone with no business membership at all.

Business admin only (`require_business_admin`, all scoped to the caller's
own business):

- `POST /my-business/members` — `{"email", "password"}` creates a brand
  new user account as a member of this business.
- `DELETE /my-business/members/{user_id}` — removes them from the
  business (does **not** delete the underlying platform account — that
  stays a platform-admin action).
- `PUT /my-business/members/{user_id}/password` — resets a member's login
  password directly.
- `POST /my-business/members/{user_id}/impersonate` — "view as" that
  member; reuses `auth_core`'s existing impersonation token/cookie
  mechanism and `/auth/admin/stop-impersonating` to exit.
- `POST /my-business/members/{user_id}/reports/{google|dropbox}/email` —
  emails that member's personal usage report to an address of the admin's
  choosing, without impersonating them.

There's no self-serve way to create a business or become a business
admin — a platform admin sets that up via the endpoints above, the same
design choice `auth_core.make_admin` makes for platform admins.

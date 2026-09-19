# auth_core

Self-contained email/password auth for FastAPI: signup, login, JWT-in-cookie
sessions. Owns its own DB connection and config — no imports from the host
app anywhere in this folder.

## Using it in a new app

1. Copy the whole `shared/auth_core/` folder (and the `shared/__init__.py`
   package marker) into the new project's backend root, next to its `app/`.
2. Install: `fastapi`, `sqlalchemy`, `pyjwt`, `passlib[bcrypt]`,
   `pydantic-settings`, plus a DB driver (`psycopg2-binary` for Postgres,
   etc).
3. Add to that app's `.env`:
   ```
   AUTH_DATABASE_URL=postgresql://user:pass@host:port/dbname
   AUTH_SECRET_KEY=<generate a new random secret per app — never reuse one>
   AUTH_ALGORITHM=HS256                  # optional, defaults to HS256
   AUTH_ACCESS_TOKEN_EXPIRE_MINUTES=60   # optional
   AUTH_COOKIE_NAME=access_token         # optional
   AUTH_COOKIE_SECURE=false              # optional, defaults to true — set
                                          # false only for local HTTP dev;
                                          # HTTPS deployments should leave
                                          # this unset/true
   ```
4. In the app's entrypoint:
   ```python
   from shared.auth_core.db import init_db
   from shared.auth_core.routes import router as auth_router

   app.include_router(auth_router, prefix="/auth")

   @app.on_event("startup")
   def _init_auth_db():
       init_db()  # creates the users table if it doesn't exist yet
   ```

That's it — `/auth/signup`, `/auth/login`, `/auth/me`, and `/auth/logout`
are live, and `shared.auth_core.dependencies.get_current_user` /
`get_optional_user` are ready to use as FastAPI dependencies elsewhere in
that app.

## Admin users

`User.is_admin` (default `False`) backs `shared.auth_core.dependencies.
require_admin`, a dependency other modules (like `subscriptions_core`) use
to gate admin-only endpoints. There's no API to promote a user — that would
be a privilege-escalation hole — so bootstrap your first admin directly:

```
python -m shared.auth_core.make_admin someone@example.com
```

If you're adding this to an app whose `users` table already exists (i.e.
`init_db()` won't add the column — `create_all` only creates missing
tables, not missing columns), add it manually once:

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false;
```

## Using tokens from other services

`shared.auth_core.security.decode_access_token(token)` verifies a token's
signature and expiry and returns its claims (`{"sub": <user id>, "exp": ...}`)
with no database access. Any service configured with the same
`AUTH_SECRET_KEY`/`AUTH_ALGORITHM` can call this to trust a token issued by
this module — e.g. a separate internal API that just needs to know which
user is calling, without going through this module's DB layer.

`shared.auth_core.dependencies.get_token_payload` is the FastAPI-dependency
form of the same check (reads the token from the `AUTH_COOKIE_NAME` cookie).
Use `get_current_user` instead when the full `User` row is also needed.

## Security posture

- **SQL injection**: all queries go through the SQLAlchemy ORM with bound
  filters — no raw/string-built SQL anywhere in this module.
- **Passwords**: hashed with bcrypt via passlib (adaptive, salted).
- **JWT**: `algorithms=[settings.ALGORITHM]` is always passed explicitly to
  `jwt.decode`, so it can't be tricked into accepting an unexpected
  algorithm.
- **Password policy**: `SignupRequest` rejects passwords shorter than
  `AUTH_MIN_PASSWORD_LENGTH` (default 8).
- **Rate limiting**: `/signup` and `/login` are limited to
  `AUTH_SIGNUP_RATE_LIMIT`/`AUTH_LOGIN_RATE_LIMIT` attempts per
  `AUTH_RATE_LIMIT_WINDOW_SECONDS` per client IP (defaults: 5 per 60s). This
  is in-process memory — fine for a single instance, but each worker/process
  tracks its own count, so a multi-worker deployment gets `limit * workers`
  effective throughput. Swap `shared/auth_core/rate_limit.py` for a
  shared-store limiter (e.g. Redis-backed) if that matters for your
  deployment.
- **Timing/enumeration**: `authenticate_user` runs a dummy bcrypt verify
  when the email doesn't exist, so a login attempt takes about the same
  time whether or not the account exists.
- **Cookie**: `httponly` always; `secure` defaults to `true` (see
  `AUTH_COOKIE_SECURE` above).

Not handled here, by design — add at the host-app level if needed: account
lockout after repeated failures, token revocation/logout before expiry, and
CSRF tokens on the login/signup forms (partially mitigated already by
`SameSite=Lax`).

## Notes

- Each app should get its own `AUTH_SECRET_KEY` unless you deliberately want
  two apps to accept each other's tokens.
- This module's `users` table lives on its own `Base`/engine
  (`shared.auth_core.db.Base`), independent of any other models the host
  app has — `init_db()` only ever manages this table.

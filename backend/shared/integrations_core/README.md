# integrations_core

Lets a logged-in user connect their Google Drive, Dropbox, and/or
Microsoft OneDrive account, see connection status, and pull a
file/storage report (used today to display data about what's in each
account; the stated long-term goal is using this as the basis for a
migration tool between providers).

`shared/tenants_core` reuses this module's Google/Dropbox/Microsoft OAuth
app credentials for its own org-wide admin connectors (a second redirect
URI + a broader scope on the same registered app) — see that module's
README before assuming its credentials are separate.

## Dependency on auth_core

Like `subscriptions_core`, this module is **not** standalone — its routes
use `shared.auth_core.dependencies.get_current_user`. A user must already
be logged into your app before they can connect an external account; this
is "link an account to my profile," not "sign in with Google."

## Using it in a new app

1. Copy `shared/integrations_core/` (and `shared/auth_core/`) into the new
   project.
2. Install: `google-auth`, `google-auth-oauthlib`, `google-api-python-client`,
   `dropbox`, `msal`.
3. Add to `.env`:
   ```
   INTEGRATIONS_DATABASE_URL=postgresql://user:pass@host:port/dbname

   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/connect/google/callback

   DROPBOX_APP_KEY=...
   DROPBOX_APP_SECRET=...
   DROPBOX_REDIRECT_URI=http://127.0.0.1:8000/connect/dropbox/callback

   MICROSOFT_CLIENT_ID=...
   MICROSOFT_CLIENT_SECRET=...
   MICROSOFT_REDIRECT_URI=http://127.0.0.1:8000/connect/microsoft/callback
   MICROSOFT_TENANT=common

   # optional, only needed for the "email me this report" endpoints
   EMAIL_ADDRESS=you@gmail.com
   EMAIL_PASSWORD=<gmail app password, not your real password>
   ```
   Provider credentials are **optional** at import time — the app won't
   crash if they're missing, but `/connect/google`, `/connect/dropbox`, and
   `/connect/microsoft` return `503` until they're set.
   `INTEGRATIONS_DATABASE_URL` is required.

   You'll need to actually create OAuth apps to get these values:
   - Google: [Google Cloud Console](https://console.cloud.google.com/) →
     APIs & Services → Credentials → OAuth client ID (Web application).
     Add the exact redirect URI above under "Authorized redirect URIs."
     Enable the Drive API for the project.
   - Dropbox: [Dropbox App Console](https://www.dropbox.com/developers/apps)
     → create an app with the `files.metadata.read`/`sharing.read` scopes.
     Add the exact redirect URI above under "Redirect URIs."
   - Microsoft: [Azure Portal](https://portal.azure.com/) → App
     registrations → New registration. Choose "Accounts in any
     organizational directory and personal Microsoft accounts," add the
     exact redirect URI above as a "Web" platform redirect URI, and grant
     the delegated Graph API permissions `openid`, `offline_access`,
     `User.Read`, `Files.ReadWrite`. If `tenants_core` is also in use, add
     its second redirect URI to this SAME app registration rather than
     creating a separate one — see that module's README.
4. In the app's entrypoint:
   ```python
   from shared.integrations_core.db import init_db as init_integrations_db
   from shared.integrations_core.routes import router as integrations_router

   app.include_router(integrations_router, prefix="/connect")

   @app.on_event("startup")
   def _init_integrations_db():
       init_integrations_db()
   ```

## Endpoints (all under `/connect`, all require login)

- `GET /connect/status` — `[{"provider": "google", "connected": true}, ...]`
- `GET /connect/{google,dropbox,microsoft}` — starts that provider's OAuth
  flow (redirect).
- `GET /connect/{google,dropbox,microsoft}/callback` — OAuth redirect
  target; must exactly match what's registered with the provider.
- `POST /connect/{google,dropbox,microsoft}/disconnect`
- `GET /connect/{google,dropbox,microsoft}/report` — storage usage,
  file/folder counts, sharing audit (public/shared/private), a category
  breakdown, and the full file list. Microsoft's "anyone with the link"
  detection comes from Graph's per-item `permissions` (`link.scope ==
  "anonymous"`), fetched via `$expand` on the same listing call rather
  than a separate request per file.
- `POST /connect/{google,dropbox,microsoft}/report/email` —
  `{"email": "..."}`, emails the report as plain text (requires
  `EMAIL_ADDRESS`/`EMAIL_PASSWORD`).

Platform-admin only (`auth_core`'s `require_admin`), no impersonation
needed:

- `POST /connect/admin/users/{user_id}/{google,dropbox,microsoft}/report/email`

## What changed from the pasted-in version

The code this was rebuilt from had real bugs worth knowing about if you're
comparing: the old `oauth_router.py` defined `google_auth`/`dropbox_auth`/
`google_callback`/`dropbox_callback` twice in the same file — Python
silently keeps only the second definition, so the first (manual
`requests`-based token exchange) was completely dead code. The actually-
running logic was the `Flow`/`DropboxOAuth2Flow`-based versions, which is
what this module ports. Token/OAuth-state storage now goes through
FastAPI's `Depends(get_db)` instead of each function opening and closing
its own `SessionLocal()`, matching how the rest of this codebase accesses
the database.

## Not included

Redis-backed login lockout (from the pasted `session lockout` code) is a
separate concern — brute-force protection on `/auth/login`, not an OAuth
integration — and wasn't ported here. `auth_core` already rate-limits
login attempts in-memory; ask if you want the Redis-backed version wired
in on top of that.

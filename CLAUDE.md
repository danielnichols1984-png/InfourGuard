# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Backend (from `backend/`, with the repo-root `venv` active):
```
source ../venv/bin/activate
uvicorn app.main:app --reload    # serves on http://localhost:8000
```
Requires a local Postgres instance reachable with the credentials in `backend/.env` (gitignored — not present in a fresh checkout; you'll need to create it, see "Config" below). Each `shared/*_core` module creates its own tables automatically on startup via `init_db()`, but does **not** alter existing tables — there's no migration tool, so adding a column to an already-existing table needs a manual `ALTER TABLE`.

Bootstrap the first admin user (there's no self-serve promotion endpoint, by design):
```
python -m shared.auth_core.make_admin <email>
```

Frontend (from `frontend/`):
```
npm run dev        # Vite dev server on http://localhost:5173
npm run build
npm run lint
```

No backend test suite exists yet (no pytest config, no test files).

## Architecture

### Two parallel UIs, one backend

The backend serves two independent frontends against the same JSON API:
- **React SPA** (`frontend/`) — the primary UI, cross-origin against the backend (CORS allows `localhost:5173`/`127.0.0.1:5173` with credentials). `frontend/src/lib/api.js` is the shared axios instance (`withCredentials: true`, required for the auth cookie).
- **Server-rendered Jinja pages** (`backend/app/web/templates/`, routed directly in `backend/app/main.py`) — a same-origin, no-build-step way to exercise the same backend without running the Vite toolchain. Login/signup/dashboard/plans/admin/integrations pages all exist here too, hitting the same underlying endpoints (some via plain form posts, some via small inline `fetch()` scripts for endpoints that only accept JSON).

Both consume the same `auth_core`/`subscriptions_core`/`integrations_core` routers — there's no separate API layer per frontend.

### `shared/*_core`: portable, self-contained modules

The core architectural pattern of this backend: each feature area lives in `backend/shared/<name>_core/` as a module designed to be copied wholesale into another FastAPI project. Each one owns:
- its own `config.py` (a `pydantic-settings` class reading its own env vars, so dropping the module into a new app never collides with that app's settings),
- its own `db.py` (own SQLAlchemy engine, `Base`, `get_db()`, `init_db()`) — these are three **separate** engines/connections, not a shared one, even though today they all point at the same local Postgres database via different `*_DATABASE_URL` env vars. That's intentional: it keeps the modules decoupled and independently portable, not an oversight.
- `models.py` / `schemas.py` / `service.py` / `routes.py` following the same shape throughout.

Each module has its own `README.md` with exact drop-in instructions (env vars, install steps, host-app wiring) — read those before modifying a module's public contract.

The three modules, in dependency order:
1. **`auth_core`** — fully standalone (imports nothing from the host app or other modules). Email/password signup/login/logout, JWT-in-cookie sessions, `User.is_admin`, in-memory rate limiting, password policy, timing-safe login (dummy bcrypt verify on unknown email to avoid enumeration). `AUTH_*` env vars.
2. **`subscriptions_core`** — plan tracking (Free/Pro/Team, editable), no real payment processing. Depends on `auth_core.dependencies.get_current_user`/`require_admin`. `SUBSCRIPTIONS_*` env vars.
3. **`integrations_core`** — Google Drive / Dropbox account connection (OAuth), storage/file report generation, "email me this report." Depends on `auth_core` for the same reason. `INTEGRATIONS_DATABASE_URL` plus unprefixed provider env vars (`GOOGLE_CLIENT_ID`, `DROPBOX_APP_KEY`, `EMAIL_ADDRESS`, etc. — deliberately not prefixed, since these are recognizable standard names). Provider credentials are optional at import time; `/connect/google` and `/connect/dropbox` return `503` rather than crashing the app if unconfigured.

`backend/app/config/settings.py` and `backend/app/db/session.py` predate this module structure and are **not imported by any current code** — don't extend them; they're dead weight left over from an earlier iteration.

### OAuth callback: state carries identity, not the session cookie

`integrations_core`'s Google/Dropbox callbacks (`google_callback`/`dropbox_callback` in `shared/integrations_core/routes.py`) deliberately do **not** depend on `get_current_user`. Chrome (and other modern browsers) treat an entire redirect chain as cross-site once any hop in it was cross-site — so a `SameSite=Lax` session cookie gets dropped not just on the provider's redirect back, but even on a same-origin redirect *from* that callback to another page on this site. Two consequences baked into the design:
- The user's id travels in the OAuth `state` parameter itself (`f"{csrf_token}:{user_id}"` for Google; Dropbox's SDK has a built-in `url_state` mechanism used the same way), verified against a per-user CSRF token stored server-side — not read from a cookie.
- The callback renders an actual HTML page (`_redirect_after_connect()`, a 200 with `<meta http-equiv="refresh">`) instead of returning an HTTP redirect, to end the cross-site navigation chain before sending the browser onward to a page that needs the auth cookie.

If a future change reintroduces `Depends(get_current_user)` on either callback, or swaps the meta-refresh page back for a plain `RedirectResponse`, it will silently break in exactly this way (works in `curl`, fails in a real browser) — this is not obvious from the code alone without this context.

### Deployment: one Render Web Service, built from the root `Dockerfile`

Production is a single Render Web Service built from the repo-root `Dockerfile` (multi-stage: `node` stage runs `npm run build` in `frontend/`, then that `dist/` is copied into the `python` stage alongside the backend). `render.yaml` is the Blueprint (one web service + one Postgres, all six `*_DATABASE_URL` env vars pointed at that same database). This deliberately avoids a second Render service for the frontend — same origin means no CORS/cross-site-cookie complexity in production.

Because of that, the built SPA is served at **`/app`**, not `/`: the Jinja pages already own `/login`, `/dashboard`, `/plans`, etc. at root, and the SPA's own `react-router` routes use those exact same path names. `vite.config.js` sets `base: '/app/'` for the production build only (the `npm run dev` dev server still serves at `/`), `App.jsx`'s `BrowserRouter` reads that same base as its `basename`, and `frontend/src/lib/api.js` uses a relative (same-origin) axios `baseURL` in production vs. the absolute `http://127.0.0.1:8000` used against the dev server. `app/main.py` mounts the built `frontend/dist` under `/app` with a catch-all route that serves real files (e.g. `favicon.svg`) as-is and falls back to `index.html` for client-side routes — skipped entirely if `frontend/dist` doesn't exist, which is the normal state for local backend-only dev.

**Local editing is unaffected**: `backend/.env` and `frontend/`'s dev workflow (`npm run dev` / `uvicorn --reload`) are untouched by any of this — `.env` stays gitignored and local-only. Render's environment variables (set in its dashboard, or as `sync: false` placeholders in `render.yaml` that it prompts for on first deploy) are a completely separate set of values (production OAuth redirect URIs pointing at the Render URL, the Render Postgres connection string) — see `backend/.env.example` for the full list and what each one is for. Each OAuth provider (Google/Dropbox/Microsoft) needs the Render redirect URI added as a *second* registered redirect URI alongside the existing localhost one on the same app registration — not a separate app.

### Content negotiation on `/auth/login`, `/auth/logout`, and `/auth/signup`

All three endpoints serve two different clients from one route: a JSON/SPA client (detected via `Accept: application/json`) gets a JSON response and no redirect; anything else (a plain HTML `<form>` submit) gets the legacy redirect-based flow. `shared/auth_core/routes.py`'s `_wants_json()` is the switch. Signup joined this pattern so the signup page's JS can keep going after account creation (log in, then optionally submit a plan checkout) without a page navigation in between — see the signup page's plan-selection flow in `backend/app/web/templates/signup.html` / `frontend/src/pages/Signup.jsx`. Keep this in mind before "simplifying" any of the three to only do one or the other — both frontends described above depend on it.

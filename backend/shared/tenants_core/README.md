# tenants_core

Organization-wide admin metrics: connect a Google Workspace, Microsoft
365, or Dropbox Business **admin** account and see tenant-level numbers —
total users, storage used/available, and (where the platform's API
actually supports it) sharing exposure — rather than one person's own
files.

## Status: Microsoft 365 verified; Google Workspace and Dropbox Business are not

`microsoft_graph.py` has been tested against a real Microsoft 365
business tenant: the OAuth flow, `/organization`, `/users/$count`,
`/subscribedSkus` (license seat counts), and the
`getOneDriveUsageAccountDetail` CSV column names all confirmed correct
against live responses. Two real bugs were found and fixed this way —
a stray reserved MSAL scope (`openid`/`offline_access` can't be passed
explicitly, MSAL adds them itself) and the "notSupported" Graph error
`integrations_core`'s sibling personal-OneDrive connector also hit —
see that module for details. Storage/OneDrive usage numbers will read
zero until at least one user in the tenant has a license assigned
(OneDrive isn't provisioned without one), and even then Microsoft's
usage reports lag real-time by 24-48h — both expected, not bugs, and the
report says so via a warning rather than presenting a fabricated number.

`google_admin.py` and `dropbox_team.py` are still unverified — built and
tested against a real, running account before being called done. This
module is the one exception: it was built with no admin/business account
available to test against for either platform. The OAuth mechanics reuse
patterns already proven to work elsewhere in this codebase, so those
parts are on reasonably solid ground. The actual admin/reporting API
calls are built from documented API shapes, not verified live — field
names, CSV column headers, and report periods may be wrong. Each module
is written to fail one metric at a time (a warning in the response)
rather than the whole report, specifically so a wrong field name is easy
to spot and fix rather than an opaque crash.

**Before offering Google Workspace or Dropbox Business to a real
customer**, verify them against a real tenant the same way Microsoft 365
just was. Google Workspace and Dropbox Business don't have a free-forever
sandbox option; a time-limited trial or a low-cost paid subscription is
the practical way to get a real tenant to test against for those two.

### Security & governance report (Microsoft 365 only)

`generate_security_report()` adds MFA coverage, approximate inactive-
account buckets, guest/external-domain counts, per-site storage, tenant-
wide external sharing exposure, directory audit events, and a storage
growth rate (from `TenantStorageSnapshot`, since Graph has no "rate"
endpoint — accumulated over repeated report fetches). It's a **separate,
on-demand endpoint** (`GET /tenants/microsoft365/security-report`) from
the fast `/report` used on page load, since it walks every SharePoint
site's drive — the same kind of per-item work that made the personal
OneDrive report take 90+ seconds before switching to `delta`.

Confirmed live against a real tenant, two real licensing/scope walls —
not bugs, not fixable in code:
- **MFA registration coverage needs Azure AD Premium P1/P2** — Graph
  returns a distinct `RequestFromNonPremiumTenantOrB2CTenant` error
  regardless of scope. `generate_security_report()` detects this specific
  error and reports it accurately rather than as a generic failure.
- **Permission drift (excessive owners / broken inheritance) was
  attempted and dropped.** `/sites/{id}/permissions` 403s under
  `Sites.Read.All` — it needs `Sites.FullControl.All`, full read/write
  control over all SharePoint content, which wasn't requested since it's
  a large trust escalation for one governance metric. Revisit only if
  that trade-off becomes worth it.
- **Directory audit events are admin/directory actions only** (user,
  role, app, group changes) — confirmed working live. File-level sharing
  and deletion events live in the separate Office 365 Management
  Activity API (its own OAuth resource, not part of Microsoft Graph),
  not covered here.
- **Inactive accounts are approximated** from OneDrive usage-report
  "Last Activity Date," not true sign-in data (`signInActivity` also
  needs Premium) — a user active in email/Teams but not OneDrive would
  still show up as inactive.

Requires the `AuditLog.Read.All` scope (added alongside this feature) —
a `TenantConnection` from before this was added needs to reconnect via
`/tenants` before these metrics work; Graph doesn't retroactively upgrade
an already-issued token's granted scope.

## Dependency on auth_core and integrations_core

Like `subscriptions_core`/`integrations_core`/`migrations_core`, this
module depends on `auth_core` (`get_current_user` — any logged-in user can
connect their own org's admin account, this isn't gated to *our*
platform's admins). It also reads `integrations_core`'s config directly
for all three providers, since each one **reuses the same OAuth app** as
the personal-account integration already built — same Google Cloud OAuth
client, same Dropbox app, same Azure AD app registration, just a second
redirect URI and a broader scope each.

## Setup

See the `.env` comments for exact redirect URIs and required scopes per
provider. Summary of what each one needs beyond the API credentials:
- **Google Workspace**: enable the Admin SDK API on the same Google Cloud
  project, add the second redirect URI to the existing OAuth client.
- **Microsoft 365**: add `MICROSOFT_ADMIN_REDIRECT_URI` as a second "Web"
  redirect URI on the same Azure AD app `integrations_core`'s personal
  OneDrive connector uses, with admin consent granted for
  `Organization.Read.All`, `User.Read.All`, `Reports.Read.All`,
  `Sites.Read.All` in addition to the personal connector's scopes — see
  `integrations_core/README.md` for the app registration itself.
- **Dropbox Business**: add the second redirect URI to the existing
  Dropbox app; enable "Team member file access" under that app's
  Permissions tab if you want storage totals (not just user/team counts).

## Endpoints (mounted under `/tenants`, all require login)

- `GET /tenants/status`
- `GET /tenants/{google-workspace,microsoft365,dropbox-business}` — starts
  that provider's admin OAuth flow.
- `GET /tenants/.../callback` — OAuth redirect target.
- `POST /tenants/.../disconnect`
- `GET /tenants/.../report` — the tenant metrics.
- `GET /tenants/microsoft365/security-report` — the slower Security &
  Governance report described above (Microsoft 365 only, on demand).

## Known gaps, stated honestly rather than silently missing

- **"How much is shared externally"** — the metric explicitly asked for —
  is the weakest-supported one across all three platforms. None of them
  expose a simple tenant-wide "externally shared item count" the way they
  expose user counts or storage totals. Getting it accurately would mean
  enumerating sharing permissions per file/site/member (expensive at real
  scale) or using each platform's separate compliance/security API
  (Google's security investigation tool, Microsoft Purview, Dropbox's
  admin console exports) — none of which is implemented here. The report
  responses say this explicitly (`external_sharing_note`) rather than
  showing a fabricated number.
- **This is metrics only** — no bulk tenant-wide migration and no email
  migration yet. Both were explicitly deferred to build this piece first;
  the existing `migrations_core` engine already supports multiple user
  mappings per job, so tenant-wide migration is really "enumerate the
  tenant's users automatically instead of typing them in one at a time" —
  a real but bounded extension once this piece is verified working. Email
  migration is a different product entirely (different APIs, different
  data model — messages/folders/attachments, not files) and hasn't been
  started.

# migrations_core

Migrates files and folders between a user's connected Google Drive,
Dropbox, and/or Microsoft OneDrive accounts (any direction between any
two), with folder/user mapping, a pre-stage dry run, a full run with
hash-verified copies, best-effort share-link recreation, reports, email
notification, and archiving.

## Dependency on auth_core and integrations_core

This module depends on both:
- `shared.auth_core` — `get_current_user`/`User.is_admin` for who's allowed to do what.
- `shared.integrations_core` — every provider credential and file operation
  (`get_credentials`/`get_client`, tree listing, download/upload, hashing,
  sharing) goes through it. `shared/migrations_core/adapters.py` is the
  translation layer between this module's provider-agnostic engine and
  integrations_core's Google/Dropbox/Microsoft-specific functions.

Copy all three modules together; this one won't run standalone.

## The model

- **Job** — one source provider, one destination provider, created by a
  user or an admin.
- **User mapping** (one or more per job) — a source user + source root path
  paired with a destination user + destination root path. A regular user
  can only map themselves (`source_user_id == destination_user_id ==
  their own id`) — migrating their own Dropbox to their own Google Drive,
  say. An admin can map *any* users to each other, e.g. consolidating
  several departing employees' Dropbox accounts into one person's Google
  Drive. Both users must have already connected the relevant provider via
  `integrations_core`'s `/connect` flow — this module never asks anyone
  for credentials itself.
- **Items** — one row per file/folder, created by pre-stage as a frozen
  snapshot (relative path, the provider-native ref, its native hash, and
  a sharing snapshot) and then updated in place as the full run processes
  it. Re-running the full run only touches items still in `planned`/
  `failed` status, so a partially-failed job can just be re-run.

## Lifecycle

`draft` → `mapped` (mappings added) → `prestaging` → `prestaged` →
`running` → `completed` / `completed_with_errors` / `failed` → `archived`.

1. `POST /migrations/jobs` — `{source_provider, destination_provider,
   notify_email?}`.
2. `POST /migrations/jobs/{id}/mappings` — add one or more user mappings.
3. `POST /migrations/jobs/{id}/prestage` — background task: lists the full
   source tree under each mapping's root and plans destination paths
   (a straight mirror of the relative structure) without transferring any
   bytes. Poll `GET /migrations/jobs/{id}/mappings/{mapping_id}/items` or
   `GET /migrations/jobs/{id}` for progress/counts.
4. `POST /migrations/jobs/{id}/run` — background task: the actual copy.
   Requires the job to be `prestaged` first.
5. `GET /migrations/jobs/{id}/report` — a text summary (also emailed
   automatically to `notify_email` when the run finishes, and available
   on demand via `POST /migrations/jobs/{id}/report/email`).
6. `POST /migrations/jobs/{id}/archive` — marks the job archived in our
   own database. **This never touches the source provider's actual
   files** — it doesn't move, rename, or delete anything on Dropbox or
   Google Drive. If you also want the source files archived/quarantined
   after a successful migration, that's a deliberate follow-up feature,
   not something this does implicitly.

`POST /migrations/jobs/{id}/recreate-share-links` re-attempts sharing on
already-copied files that don't have it yet, without re-copying anything —
useful after fixing a permissions issue.

## Integrity verification

Every file copy is hash-verified on **both** legs, using each provider's
own native, already-documented hash — no extra API calls or double
downloads needed:
- **Download leg**: after downloading from source, the bytes are hashed
  locally with the source provider's algorithm and compared to that
  provider's own reported hash (Google's `md5Checksum` off the file
  listing; Dropbox's `content_hash`, computed via
  `dropbox_integration.compute_dropbox_content_hash` — SHA-256 of each
  4MB block, then SHA-256 of the concatenated block hashes, per Dropbox's
  published spec; Microsoft's `quickXorHash`, via the third-party
  `quickxorhash` package — verified against a real OneDrive file's
  Graph-reported hash before being trusted here).
- **Upload leg**: after uploading, the same local hash is compared to the
  hash the *destination* reports back for what it just stored.

`MigrationItem.verified` is only `True` if both checks pass; a mismatch on
either leg marks the item `failed` with a specific error rather than
silently trusting either provider.

## Known limitations (honest MVP scope, not accidental gaps)

- **Folder mapping is a straight mirror**: everything under a mapping's
  source root lands at the same relative path under the destination root.
  There's no per-subfolder rename/redirect rule engine — if you need one
  subfolder to land somewhere unrelated to the rest, that's a second
  mapping with its own root paths, or a feature to add later.
- **Named-person share recreation** only works from a Google source (its
  `permissions` field already lists exact grantee emails, fetched for free
  during listing) — Dropbox-as-source and Microsoft-as-source can't
  recreate named grants without extra per-file API calls (not
  implemented, and for Microsoft specifically the same expensive-at-scale
  per-item call that turned out to be both slow *and* wrong for sharing
  *detection* — see `integrations_core/microsoft.py`). Onto a Google or
  Microsoft *destination*, a named grant is attempted for real via that
  provider's own single-item invite API and reports whatever that API
  actually returns (Microsoft's specifically is unverified against a
  second real recipient — see that adapter's `apply_named_sharing`).
  Onto a Dropbox destination it can't be done at all via a single API
  call. "Anyone with link" recreates cleanly onto any destination from
  any source.
- **Runs in-process** via FastAPI `BackgroundTasks` — no separate worker,
  no resumability across a server restart mid-run (a restart mid-job
  leaves it in `running` with whatever items already completed; re-running
  `/run` picks back up from the still-`planned`/`failed` items once the
  process is back up, but nothing resumes automatically).
- **Whole files in memory**: a file is downloaded fully into memory before
  being uploaded — fine for typical files, but very large files will cost
  proportional memory. Dropbox (150MB) and Microsoft (4MB, Graph's own
  documented safe threshold) chunk their uploads past that size; Google's
  upload path does not chunk.

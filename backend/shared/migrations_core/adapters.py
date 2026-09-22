"""Provider-agnostic adapter layer.

The migration engine (service.py) is written once against this interface;
GoogleAdapter and DropboxAdapter each translate it onto the very different
APIs of shared.integrations_core.google / .dropbox_integration. Each
adapter opens its own short-lived integrations_core DB session internally
to resolve a user's stored OAuth tokens — migrations_core never touches
integrations_core's tables through its own `db` session, keeping the two
modules' database engines properly separate even though, today, they
happen to point at the same physical database.
"""
from shared.integrations_core import dropbox_integration, google as google_integration, microsoft as microsoft_integration
from shared.integrations_core import service as integrations_service
from shared.integrations_core.db import SessionLocal as IntegrationsSessionLocal


def user_has_provider_connected(user_id: int, provider: str) -> bool:
    idb = IntegrationsSessionLocal()
    try:
        return integrations_service.is_connected(idb, user_id, provider)
    finally:
        idb.close()


def user_has_destination_write_access(user_id: int, provider: str) -> bool:
    """Whether user_id's connection to `provider` can actually be used as a
    migration destination (create files/folders, set sharing) — not just
    whether it's connected at all. Only Google currently needs this check:
    its write access is a specific OAuth scope that a token connected
    before that scope was required won't have, and Google never
    retroactively upgrades an already-issued token. Dropbox's permissions
    are configured once on the whole app in its console, not requested
    per-connection, so there's no per-token scope to fall out of date here
    the same way."""
    if provider != "google":
        return True

    idb = IntegrationsSessionLocal()
    try:
        return google_integration.has_write_scope(idb, user_id)
    finally:
        idb.close()


class ProviderAdapter:
    def get_client(self, user_id: int, container_type: str | None = None, container_id: str | None = None):
        """user_id is the migrating individual for a personal-account
        mapping (container_type is None, the original and still-default
        behavior). For a business-storage mapping (container_type is
        "shared_drive" / "site" / "team_folder"), user_id is instead the
        ADMIN whose tenant connection performs the work, and container_id
        is that container's provider-native id — see each concrete
        adapter for exactly which tenant connection it pulls from."""
        raise NotImplementedError

    def resolve_root(self, client, root_path: str, container_type: str | None = None, container_id: str | None = None):
        """Resolves an existing path to a provider-native ref. Raises if it
        doesn't exist — used for the *source* side, which must already
        exist."""
        raise NotImplementedError

    def list_tree(self, client, root_ref) -> list[dict]:
        """Returns a flat list of dicts with at least: relative_path,
        is_folder, size, native_hash, ref, mime_type, export_mime_type,
        skip_reason, and sharing fields (shape varies by provider — see
        recreate_sharing). `export_mime_type` is only ever set for a
        Google-native source file (see GoogleAdapter). `skip_reason`, when
        set, means don't even attempt this item — there's no content to
        copy (e.g. a Google Form)."""
        raise NotImplementedError

    def ensure_child_folder(self, client, parent_ref, name: str):
        raise NotImplementedError

    def ensure_path(self, client, path: str, container_type: str | None = None, container_id: str | None = None):
        """Resolves a path to a provider-native ref, creating any missing
        segments — used for the *destination* side."""
        ref = self.resolve_root(client, "", container_type, container_id)
        for segment in [s for s in path.strip("/").split("/") if s]:
            ref = self.ensure_child_folder(client, ref, segment)
        return ref

    def download(self, client, ref, export_mime_type: str | None = None) -> bytes:
        raise NotImplementedError

    def upload(self, client, parent_ref, name: str, data: bytes, mime_type: str | None) -> dict:
        """Returns {"ref": ..., "hash": ...}."""
        raise NotImplementedError

    def compute_hash(self, data: bytes) -> str:
        raise NotImplementedError

    def apply_public_sharing(self, client, ref) -> None:
        raise NotImplementedError

    def apply_named_sharing(self, client, ref, email: str) -> bool:
        """Returns False if this provider doesn't support per-file named
        sharing (rather than raising) — callers treat that as "skipped",
        not an error."""
        raise NotImplementedError


class GoogleAdapter(ProviderAdapter):
    def get_client(self, user_id, container_type=None, container_id=None):
        """Returns (drive_service, drive_id) — drive_id is None for a
        personal My Drive mapping, or the Shared Drive's id for a
        business-storage one, threaded through to every
        integrations_core.google function that needs corpora="drive"/
        supportsAllDrives (see that module for details). A "shared_drive"
        container pulls credentials from the tenant admin's Workspace
        connection (tenants_core), not the migrating user's own personal
        connection — this only works because that admin connection was
        deliberately upgraded with domain-wide Drive access."""
        if container_type == "shared_drive":
            from shared.tenants_core.db import SessionLocal as TenantsSessionLocal
            from shared.tenants_core import google_admin

            tdb = TenantsSessionLocal()
            try:
                creds = google_admin.get_credentials(tdb, user_id)
            finally:
                tdb.close()
            if not creds:
                return None
            return (google_integration.build_drive_service(creds), container_id)

        idb = IntegrationsSessionLocal()
        try:
            creds = google_integration.get_credentials(idb, user_id)
        finally:
            idb.close()
        if not creds:
            return None
        return (google_integration.build_drive_service(creds), None)

    def resolve_root(self, client, root_path, container_type=None, container_id=None):
        service, drive_id = client
        if not root_path:
            return drive_id if drive_id else "root"
        return google_integration.resolve_path_to_folder_id(service, root_path, drive_id)

    def list_tree(self, client, root_ref):
        service, drive_id = client
        tree = google_integration.list_folder_tree(service, root_ref, drive_id)
        result = []
        for f in tree:
            mime = f.get("mimeType")
            relative_path = f["relative_path"]
            mime_type = mime
            export_mime_type = None
            skip_reason = None

            if not f["is_folder"] and google_integration.is_google_native_type(mime):
                export_target = google_integration.get_export_target(mime)
                if export_target:
                    export_mime_type, extension = export_target
                    mime_type = export_mime_type
                    if not relative_path.lower().endswith(extension):
                        relative_path = f"{relative_path}{extension}"
                elif mime in google_integration.GOOGLE_NATIVE_UNEXPORTABLE_TYPES:
                    skip_reason = f"{mime.rsplit('.', 1)[-1].title()} has no exportable file content"
                else:
                    skip_reason = f"Unrecognized Google-native type ({mime}), can't export"

            result.append(
                {
                    "relative_path": relative_path,
                    "is_folder": f["is_folder"],
                    "size": int(f.get("size") or 0),
                    "native_hash": f.get("md5Checksum"),
                    "mime_type": mime_type,
                    "export_mime_type": export_mime_type,
                    "skip_reason": skip_reason,
                    "ref": f["id"],
                    "permissions": f.get("permissions") or [],
                }
            )
        return result

    def ensure_child_folder(self, client, parent_ref, name):
        service, drive_id = client
        return google_integration.ensure_folder(service, parent_ref, name, drive_id)

    def download(self, client, ref, export_mime_type=None):
        service, drive_id = client
        if export_mime_type:
            return google_integration.export_file_bytes(service, ref, export_mime_type)
        return google_integration.download_file_bytes(service, ref, drive_id)

    def upload(self, client, parent_ref, name, data, mime_type):
        service, drive_id = client
        result = google_integration.upload_file_bytes(service, parent_ref, name, data, mime_type, drive_id)
        # Drive allows duplicate names and never overwrites, so the
        # requested name is always the actual one — echoed back for
        # interface consistency with Dropbox/Microsoft, which may rename.
        return {"ref": result["id"], "hash": result.get("md5Checksum"), "name": name}

    def compute_hash(self, data):
        return google_integration.compute_md5(data)

    def apply_public_sharing(self, client, ref):
        service, drive_id = client
        google_integration.apply_public_sharing(service, ref, drive_id)

    def apply_named_sharing(self, client, ref, email):
        service, drive_id = client
        google_integration.apply_named_sharing(service, ref, email, drive_id=drive_id)
        return True


class DropboxAdapter(ProviderAdapter):
    def get_client(self, user_id, container_type=None, container_id=None):
        """A "team_folder" container returns a client already fully
        scoped to that folder's own namespace via as_admin()/
        with_path_root() — unlike Google/Microsoft, every other method
        below needs zero changes, since paths are just relative to
        whatever root this client object is already scoped to."""
        if container_type == "team_folder":
            from shared.tenants_core.db import SessionLocal as TenantsSessionLocal
            from shared.tenants_core import dropbox_team
            from dropbox.common import PathRoot

            tdb = TenantsSessionLocal()
            try:
                dbx_team = dropbox_team.get_team_client(tdb, user_id)
                if not dbx_team:
                    return None
                admin_id = dropbox_team.get_admin_team_member_id(dbx_team)
            finally:
                tdb.close()
            return dbx_team.as_admin(admin_id).with_path_root(PathRoot.namespace_id(container_id))

        idb = IntegrationsSessionLocal()
        try:
            return dropbox_integration.get_client(idb, user_id)
        finally:
            idb.close()

    def resolve_root(self, client, root_path, container_type=None, container_id=None):
        return "" if not root_path else "/" + root_path.strip("/")

    def list_tree(self, client, root_ref):
        tree = dropbox_integration.list_folder_tree(client, root_ref)
        return [
            {
                "relative_path": f["relative_path"],
                "is_folder": f["is_folder"],
                "size": int(f.get("size") or 0),
                "native_hash": f.get("content_hash"),
                "mime_type": None,
                "ref": f["path"],
                "anyone_with_link": f.get("anyone_with_link", False),
            }
            for f in tree
        ]

    def ensure_child_folder(self, client, parent_ref, name):
        child_path = f"{parent_ref}/{name}" if parent_ref else f"/{name}"
        return dropbox_integration.ensure_folder(client, child_path)

    def download(self, client, ref, export_mime_type=None):
        return dropbox_integration.download_file_bytes(client, ref)

    def upload(self, client, parent_ref, name, data, mime_type):
        dest_path = f"{parent_ref}/{name}" if parent_ref else f"/{name}"
        result = dropbox_integration.upload_file_bytes(client, dest_path, data)
        return {"ref": result["path"], "hash": result.get("content_hash"), "name": result.get("name", name)}

    def compute_hash(self, data):
        return dropbox_integration.compute_dropbox_content_hash(data)

    def apply_public_sharing(self, client, ref):
        dropbox_integration.apply_public_sharing(client, ref)

    def apply_named_sharing(self, client, ref, email):
        # Dropbox's sharing model doesn't support inviting a named person
        # to a single file the way Google's per-file permissions do (you'd
        # share the containing folder instead) — not implemented here.
        return False


class MicrosoftAdapter(ProviderAdapter):
    def get_client(self, user_id, container_type=None, container_id=None):
        """Returns (access_token, base_path) — base_path selects which
        drive every call operates against: "/me/drive" for a personal
        OneDrive mapping, or f"/sites/{container_id}/drive" for a
        business-storage one. A "site" container pulls its token from the
        tenant admin's Microsoft 365 connection (tenants_core), not the
        migrating user's own personal connection — this only works
        because that admin connection was deliberately upgraded to
        Sites.ReadWrite.All specifically for this."""
        if container_type == "site":
            from shared.tenants_core.db import SessionLocal as TenantsSessionLocal
            from shared.tenants_core import microsoft_graph

            tdb = TenantsSessionLocal()
            try:
                access_token = microsoft_graph.get_access_token(tdb, user_id)
            finally:
                tdb.close()
            if not access_token:
                return None
            return (access_token, f"/sites/{container_id}/drive")

        idb = IntegrationsSessionLocal()
        try:
            access_token = microsoft_integration.get_access_token(idb, user_id)
        finally:
            idb.close()
        if not access_token:
            return None
        return (access_token, "/me/drive")

    def resolve_root(self, client, root_path, container_type=None, container_id=None):
        access_token, base_path = client
        return microsoft_integration.resolve_path_to_item_id(access_token, root_path, base_path)

    def list_tree(self, client, root_ref):
        access_token, base_path = client
        return microsoft_integration.list_folder_tree(access_token, root_ref, base_path)

    def ensure_child_folder(self, client, parent_ref, name):
        access_token, base_path = client
        return microsoft_integration.ensure_folder(access_token, parent_ref, name, base_path)

    def download(self, client, ref, export_mime_type=None):
        access_token, base_path = client
        return microsoft_integration.download_file_bytes(access_token, ref, base_path)

    def upload(self, client, parent_ref, name, data, mime_type):
        access_token, base_path = client
        return microsoft_integration.upload_file_bytes(access_token, parent_ref, name, data, base_path)

    def compute_hash(self, data):
        return microsoft_integration.compute_quickxorhash(data)

    def apply_public_sharing(self, client, ref):
        access_token, base_path = client
        microsoft_integration.apply_public_sharing(access_token, ref, base_path)

    def apply_named_sharing(self, client, ref, email):
        # Unlike Dropbox (a real product limitation), OneDrive's /invite
        # endpoint genuinely supports single-item named sharing per
        # Graph's docs — but it's unverified against a second real
        # recipient account. Rather than either skip it outright or
        # optimistically report success, this makes the real call and
        # reports Graph's real response, so a wrong payload shape shows
        # up as "not recreated" instead of a false "recreated".
        access_token, base_path = client
        return microsoft_integration.apply_named_sharing(access_token, ref, email, base_path)


ADAPTERS: dict[str, ProviderAdapter] = {
    "google": GoogleAdapter(),
    "dropbox": DropboxAdapter(),
    "microsoft": MicrosoftAdapter(),
}


def get_adapter(provider: str) -> ProviderAdapter:
    adapter = ADAPTERS.get(provider)
    if not adapter:
        raise ValueError(f"Unknown provider: {provider!r}")
    return adapter


def recreate_sharing(source_provider: str, dest_adapter: ProviderAdapter, dest_client, dest_ref, sharing: dict) -> bool:
    """Best-effort: recreates whatever sharing we could see on the source
    item, on the destination item. Returns whether anything was recreated.

    Google source: full fidelity for "anyone with link" and named-person
    grants (both captured via the `permissions` field at prestage time).
    Dropbox source: only "anyone with link" is detectable without extra
    per-file API calls, so that's all that's recreated from a Dropbox
    source today.
    """
    if not sharing:
        return False

    recreated = False
    if source_provider == "google":
        for permission in sharing.get("permissions") or []:
            if permission.get("type") == "anyone":
                dest_adapter.apply_public_sharing(dest_client, dest_ref)
                recreated = True
            elif permission.get("type") == "user" and permission.get("emailAddress"):
                if dest_adapter.apply_named_sharing(dest_client, dest_ref, permission["emailAddress"]):
                    recreated = True
    else:
        if sharing.get("anyone_with_link"):
            dest_adapter.apply_public_sharing(dest_client, dest_ref)
            recreated = True

    return recreated

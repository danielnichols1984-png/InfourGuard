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
    def get_client(self, user_id: int):
        raise NotImplementedError

    def resolve_root(self, client, root_path: str):
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

    def ensure_path(self, client, path: str):
        """Resolves a path to a provider-native ref, creating any missing
        segments — used for the *destination* side."""
        ref = self.resolve_root(client, "")
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
    def get_client(self, user_id):
        idb = IntegrationsSessionLocal()
        try:
            creds = google_integration.get_credentials(idb, user_id)
        finally:
            idb.close()
        return google_integration.build_drive_service(creds) if creds else None

    def resolve_root(self, client, root_path):
        if not root_path:
            return "root"
        return google_integration.resolve_path_to_folder_id(client, root_path)

    def list_tree(self, client, root_ref):
        tree = google_integration.list_folder_tree(client, root_ref)
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
        return google_integration.ensure_folder(client, parent_ref, name)

    def download(self, client, ref, export_mime_type=None):
        if export_mime_type:
            return google_integration.export_file_bytes(client, ref, export_mime_type)
        return google_integration.download_file_bytes(client, ref)

    def upload(self, client, parent_ref, name, data, mime_type):
        result = google_integration.upload_file_bytes(client, parent_ref, name, data, mime_type)
        return {"ref": result["id"], "hash": result.get("md5Checksum")}

    def compute_hash(self, data):
        return google_integration.compute_md5(data)

    def apply_public_sharing(self, client, ref):
        google_integration.apply_public_sharing(client, ref)

    def apply_named_sharing(self, client, ref, email):
        google_integration.apply_named_sharing(client, ref, email)
        return True


class DropboxAdapter(ProviderAdapter):
    def get_client(self, user_id):
        idb = IntegrationsSessionLocal()
        try:
            return dropbox_integration.get_client(idb, user_id)
        finally:
            idb.close()

    def resolve_root(self, client, root_path):
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
        return {"ref": result["path"], "hash": result.get("content_hash")}

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
    def get_client(self, user_id):
        idb = IntegrationsSessionLocal()
        try:
            return microsoft_integration.get_access_token(idb, user_id)
        finally:
            idb.close()

    def resolve_root(self, client, root_path):
        return microsoft_integration.resolve_path_to_item_id(client, root_path)

    def list_tree(self, client, root_ref):
        return microsoft_integration.list_folder_tree(client, root_ref)

    def ensure_child_folder(self, client, parent_ref, name):
        return microsoft_integration.ensure_folder(client, parent_ref, name)

    def download(self, client, ref, export_mime_type=None):
        return microsoft_integration.download_file_bytes(client, ref)

    def upload(self, client, parent_ref, name, data, mime_type):
        return microsoft_integration.upload_file_bytes(client, parent_ref, name, data)

    def compute_hash(self, data):
        return microsoft_integration.compute_quickxorhash(data)

    def apply_public_sharing(self, client, ref):
        microsoft_integration.apply_public_sharing(client, ref)

    def apply_named_sharing(self, client, ref, email):
        # Unlike Dropbox (a real product limitation), OneDrive's /invite
        # endpoint genuinely supports single-item named sharing per
        # Graph's docs — but it's unverified against a second real
        # recipient account. Rather than either skip it outright or
        # optimistically report success, this makes the real call and
        # reports Graph's real response, so a wrong payload shape shows
        # up as "not recreated" instead of a false "recreated".
        return microsoft_integration.apply_named_sharing(client, ref, email)


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

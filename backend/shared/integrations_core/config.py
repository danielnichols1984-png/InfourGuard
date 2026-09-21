from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IntegrationsSettings(BaseSettings):
    """Config for integrations_core, independent of the host app's own
    settings. Provider credentials are optional at import time (unlike
    auth_core/subscriptions_core's required DB URLs) so the app doesn't
    crash on startup just because Google/Dropbox aren't set up yet — the
    connect routes check for them and fail with a clear 503 instead."""

    DATABASE_URL: str = Field(validation_alias="INTEGRATIONS_DATABASE_URL")

    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None
    # Full read/write Drive access — required so migrations_core can create
    # folders/files and set sharing on a Google destination, not just list
    # files for the read-only report. A user who connected under the old
    # drive.readonly-only scope needs to reconnect for this to take effect;
    # Google doesn't retroactively upgrade an already-issued token's scope.
    GOOGLE_SCOPES: str = (
        "openid,https://www.googleapis.com/auth/userinfo.email,"
        "https://www.googleapis.com/auth/drive"
    )

    DROPBOX_APP_KEY: str | None = None
    DROPBOX_APP_SECRET: str | None = None
    DROPBOX_REDIRECT_URI: str | None = None

    MICROSOFT_CLIENT_ID: str | None = None
    MICROSOFT_CLIENT_SECRET: str | None = None
    MICROSOFT_REDIRECT_URI: str | None = None
    # "common" allows sign-in from either a work/school or personal
    # Microsoft account — this is the personal OneDrive connector, so it
    # needs to accept both, unlike tenants_core's admin connector which
    # only makes sense for a work/school account.
    MICROSOFT_TENANT: str = "common"
    # openid/profile/offline_access are NOT listed here even though we
    # need a refresh token — MSAL's ConfidentialClientApplication adds
    # those three itself and raises ValueError("reserved scope") if
    # they're also passed explicitly. Only list the actual resource scopes.
    MICROSOFT_SCOPES: str = "User.Read,Files.ReadWrite"

    EMAIL_ADDRESS: str | None = None
    EMAIL_PASSWORD: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def google_scopes_list(self) -> list[str]:
        return [s.strip() for s in self.GOOGLE_SCOPES.split(",") if s.strip()]

    @property
    def google_configured(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET and self.GOOGLE_REDIRECT_URI)

    @property
    def dropbox_configured(self) -> bool:
        return bool(self.DROPBOX_APP_KEY and self.DROPBOX_APP_SECRET and self.DROPBOX_REDIRECT_URI)

    @property
    def microsoft_scopes_list(self) -> list[str]:
        return [s.strip() for s in self.MICROSOFT_SCOPES.split(",") if s.strip()]

    @property
    def microsoft_configured(self) -> bool:
        return bool(self.MICROSOFT_CLIENT_ID and self.MICROSOFT_CLIENT_SECRET and self.MICROSOFT_REDIRECT_URI)


settings = IntegrationsSettings()

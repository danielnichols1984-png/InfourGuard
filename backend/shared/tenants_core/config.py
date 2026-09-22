from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TenantsSettings(BaseSettings):
    """Config for tenants_core (business/organization-wide admin
    connections and metrics), independent of the host app's own settings.

    Google Workspace and Dropbox Business deliberately reuse the SAME
    OAuth app credentials as integrations_core (GOOGLE_CLIENT_ID/SECRET,
    DROPBOX_APP_KEY/SECRET) — they're the same registered app, just with a
    different redirect URI and a broader, admin-level scope requested.
    Both providers support multiple redirect URIs per app, so this needs
    one new URI added in each provider's console, not a whole new app.

    Microsoft 365 now also reuses integrations_core's OAuth app
    (MICROSOFT_CLIENT_ID/SECRET/TENANT) the same way, once
    integrations_core grew its own personal OneDrive connector — same
    Azure AD app registration, just a second redirect URI
    (MICROSOFT_ADMIN_REDIRECT_URI below) and a broader, admin-only scope.
    """

    DATABASE_URL: str = Field(validation_alias="TENANTS_DATABASE_URL")

    # Encrypts every stored OAuth secret at rest — see crypto.py.
    # Unprefixed and shared with integrations_core's identical setting
    # (same one app, one key precedent as GOOGLE_CLIENT_ID etc.).
    TOKEN_ENCRYPTION_KEY: str

    GOOGLE_ADMIN_REDIRECT_URI: str | None = None

    MICROSOFT_ADMIN_REDIRECT_URI: str | None = None

    DROPBOX_BUSINESS_REDIRECT_URI: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def google_admin_configured(self) -> bool:
        from shared.integrations_core.config import settings as integrations_settings

        return bool(
            integrations_settings.GOOGLE_CLIENT_ID
            and integrations_settings.GOOGLE_CLIENT_SECRET
            and self.GOOGLE_ADMIN_REDIRECT_URI
        )

    @property
    def microsoft_configured(self) -> bool:
        from shared.integrations_core.config import settings as integrations_settings

        return bool(
            integrations_settings.MICROSOFT_CLIENT_ID
            and integrations_settings.MICROSOFT_CLIENT_SECRET
            and self.MICROSOFT_ADMIN_REDIRECT_URI
        )

    @property
    def dropbox_business_configured(self) -> bool:
        from shared.integrations_core.config import settings as integrations_settings

        return bool(
            integrations_settings.DROPBOX_APP_KEY
            and integrations_settings.DROPBOX_APP_SECRET
            and self.DROPBOX_BUSINESS_REDIRECT_URI
        )


settings = TenantsSettings()

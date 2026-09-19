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
    GOOGLE_SCOPES: str = (
        "openid,https://www.googleapis.com/auth/userinfo.email,"
        "https://www.googleapis.com/auth/drive.readonly"
    )

    DROPBOX_APP_KEY: str | None = None
    DROPBOX_APP_SECRET: str | None = None
    DROPBOX_REDIRECT_URI: str | None = None

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


settings = IntegrationsSettings()

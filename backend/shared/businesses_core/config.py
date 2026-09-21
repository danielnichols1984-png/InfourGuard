from pydantic_settings import BaseSettings, SettingsConfigDict


class BusinessesSettings(BaseSettings):
    """Config for businesses_core, independent of the host app's own
    settings. Prefixed BUSINESSES_ so it never collides with auth_core's
    AUTH_-prefixed vars or the host app's own config."""

    DATABASE_URL: str

    model_config = SettingsConfigDict(
        env_prefix="BUSINESSES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = BusinessesSettings()

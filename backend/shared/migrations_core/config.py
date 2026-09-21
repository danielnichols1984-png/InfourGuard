from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MigrationsSettings(BaseSettings):
    """Config for migrations_core, independent of the host app's own
    settings. This module never talks to Google/Dropbox directly with its
    own credentials — it goes through integrations_core's stored tokens for
    whichever user is on each side of a migration — so the only setting it
    owns is its database connection."""

    DATABASE_URL: str = Field(validation_alias="MIGRATIONS_DATABASE_URL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = MigrationsSettings()

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ContentSettings(BaseSettings):
    """Config for content_core (admin-editable marketing copy), independent
    of the host app's own settings — same portable-module pattern as
    auth_core/subscriptions_core/etc."""

    DATABASE_URL: str = Field(validation_alias="CONTENT_DATABASE_URL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = ContentSettings()

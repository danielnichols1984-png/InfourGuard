from pydantic_settings import BaseSettings, SettingsConfigDict


class SubscriptionsSettings(BaseSettings):
    """Config for subscriptions_core, independent of the host app's own
    settings. Prefixed SUBSCRIPTIONS_ so it never collides with auth_core's
    AUTH_-prefixed vars or the host app's own config."""

    DATABASE_URL: str

    model_config = SettingsConfigDict(
        env_prefix="SUBSCRIPTIONS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = SubscriptionsSettings()

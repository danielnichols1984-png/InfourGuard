from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    """Config for auth_core, set independently of whatever app mounts it.

    All variables are prefixed AUTH_ so they never collide with a host
    app's own settings (e.g. its own SECRET_KEY/DATABASE_URL).
    """

    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    COOKIE_NAME: str = "access_token"
    COOKIE_SECURE: bool = True
    MIN_PASSWORD_LENGTH: int = 8
    LOGIN_RATE_LIMIT: int = 5
    SIGNUP_RATE_LIMIT: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = AuthSettings()

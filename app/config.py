from functools import lru_cache
import hashlib
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str
    public_base_url: str = ""
    railway_public_domain: str = ""
    webhook_secret: str = ""
    admin_ids: str = ""
    paypal_details: str = ""
    revolut_details: str = ""
    entry_price_eur: int = 2
    reentry_price_eur: int = 5
    referral_target: int = 20
    referral_window_hours: int = 48
    referral_validation_minutes: int = 5
    invite_ttl_hours: int = 24
    first_media_hours: int = 24
    activity_window_hours: int = 72
    activity_media_target: int = 5
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @property
    def admin_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_ids.split(",") if x.strip()}

    @property
    def sqlalchemy_database_url(self) -> str:
        """Accepte directement DATABASE_URL de Railway."""
        url = self.database_url.strip()
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://"):]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://"):]
        return url

    @property
    def resolved_public_base_url(self) -> str:
        value = self.public_base_url.strip().rstrip("/")
        if value:
            if not value.startswith(("http://", "https://")):
                value = "https://" + value
            return value
        domain = self.railway_public_domain.strip().rstrip("/")
        if domain:
            return "https://" + domain
        return ""

    @property
    def resolved_webhook_secret(self) -> str:
        value = self.webhook_secret.strip()
        if value:
            return value
        # Secret stable et compatible Telegram, sans exposer le token.
        return hashlib.sha256(self.bot_token.encode()).hexdigest()[:48]

    @property
    def webhook_url(self) -> str:
        base = self.resolved_public_base_url
        return f"{base}/telegram/webhook" if base else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()

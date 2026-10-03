"""All configuration in one place. Money constants are Decimal and never come from floats."""

from decimal import Decimal
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Money rules (section 5) -------------------------------------------------
RUPEE = Decimal("0.01")  # rupee amounts: 2 dp
GRAM = Decimal("0.0001")  # gold quantities: 4 dp
GST_RATE = Decimal("0.03")  # user-entered amount is GST-inclusive
MIN_PURCHASE = Decimal("10.00")
MAX_PURCHASE = Decimal("100000.00")

DEFAULT_ADMIN_TOKEN = "dev-admin-token"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://gold:gold@db:5432/gold"

    llm_provider: Literal["groq", "gemini"] = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 8.0  # a timeout, not money

    price_base: Decimal = Decimal("15131.23")  # per gram, ex-GST
    price_source_mode: Literal["up", "down"] = "up"
    price_max_age_seconds: int = 300
    quote_ttl_seconds: int = 300
    # How long a SIP proposal waits for "yes" in chat (the proposal itself stays confirmable via API).
    pending_action_ttl_seconds: int = 300

    # Calendar dates (SIP due dates, run-due) are interpreted in this zone.
    business_tz: str = "Asia/Kolkata"

    # Shared-secret stopgap for /admin/*; not real auth. ENV=production refuses demo-only
    # admin actions (clock, reset) and refuses the default token.
    admin_token: SecretStr | None = SecretStr(DEFAULT_ADMIN_TOKEN)
    env: Literal["development", "test", "production"] = "development"

    # SIP execution limits.
    max_catch_up_instalments: int = 7
    max_retry_attempts: int = 5

    log_level: str = "INFO"
    log_file: str = "logs/app.jsonl"

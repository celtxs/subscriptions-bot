from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import base64, os
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_telegram_id: int
    db_path: Path
    encryption_key_path: Path
    timezone: ZoneInfo
    scheduler_interval_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "ENCRYPTION_KEY_PATH")
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise RuntimeError("missing required configuration: " + ", ".join(missing))
        value = os.environ.get("DB_PATH", "/app/data/subscriptions.db")
        settings = cls(os.environ["BOT_TOKEN"], int(os.environ["OWNER_TELEGRAM_ID"]), Path(value), Path(os.environ["ENCRYPTION_KEY_PATH"]), ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")), int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60")), os.getenv("LOG_LEVEL", "INFO"))
        if settings.scheduler_interval_seconds < 30:
            raise RuntimeError("SCHEDULER_INTERVAL_SECONDS must be at least 30")
        return settings

    def encryption_key(self) -> bytes:
        try:
            raw = self.encryption_key_path.read_text(encoding="ascii").strip()
            key = base64.urlsafe_b64decode(raw.encode("ascii"))
        except Exception as exc:
            raise RuntimeError("encryption key unavailable") from exc
        if len(key) != 32:
            raise RuntimeError("encryption key must decode to 32 bytes")
        return key

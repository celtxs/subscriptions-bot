from __future__ import annotations
from datetime import datetime, timedelta, timezone
from app.config import Settings
from app.db.connection import connect
from app.db.schema import SCHEMA_VERSION

def main():
    settings=Settings.from_env(); settings.encryption_key(); con=connect(settings.db_path)
    integrity=con.execute("PRAGMA integrity_check").fetchone()[0]; version=con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    heartbeat=con.execute("SELECT value FROM settings WHERE key='last_scheduler_tick_utc'").fetchone()
    lease=con.execute("SELECT holder_id, expires_at_utc FROM scheduler_lease WHERE name='reminders'").fetchone()
    if integrity != "ok" or version != SCHEMA_VERSION: raise SystemExit(1)
    try:
        heartbeat_at = datetime.fromisoformat(heartbeat[0]) if heartbeat else None
        if heartbeat_at is not None and (heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None):
            heartbeat_at = None
        elif heartbeat_at is not None:
            heartbeat_at = heartbeat_at.astimezone(timezone.utc)
    except (TypeError, ValueError):
        heartbeat_at = None
    now = datetime.now(timezone.utc)
    try:
        lease_expires_at = datetime.fromisoformat(lease["expires_at_utc"]).astimezone(timezone.utc) if lease and lease["holder_id"] and lease["expires_at_utc"] else None
    except (TypeError, ValueError):
        lease_expires_at = None
    if heartbeat_at is None or heartbeat_at > now or now-heartbeat_at > timedelta(seconds=settings.scheduler_interval_seconds*3): raise SystemExit(1)
    if lease_expires_at is None or lease_expires_at <= now: raise SystemExit(1)
    print("healthy")
if __name__ == "__main__": main()

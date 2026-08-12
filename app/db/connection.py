from __future__ import annotations
import sqlite3
from pathlib import Path
from app.db.schema import SCHEMA_SQL, SCHEMA_VERSION


_ADDITIVE_COLUMNS = {
    "subscriptions": (
        ("cost_minor", "INTEGER"),
        ("currency", "TEXT"),
        ("service_url", "TEXT"),
        ("note", "TEXT"),
        ("secret_ciphertext", "BLOB"),
        ("secret_nonce", "BLOB"),
        ("encryption_key_version", "INTEGER"),
        ("replaced_from_id", "TEXT"),
        ("deactivation_reason", "TEXT"),
        ("deactivated_at_utc", "TEXT"),
    ),
    "reminder_deliveries": (
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("claimed_until_utc", "TEXT"),
        ("sent_at_utc", "TEXT"),
        ("telegram_message_id", "INTEGER"),
        ("last_error_code", "TEXT"),
    ),
}

class TransactionalConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._context_depth = 0
        self._rollback_only = False

    def __enter__(self):
        if self._context_depth == 0:
            self.execute("BEGIN")
            self._rollback_only = False
        self._context_depth += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._context_depth -= 1
        if exc_type is not None:
            self._rollback_only = True
        if self._context_depth == 0:
            try:
                if self._rollback_only:
                    self.rollback()
                else:
                    self.commit()
            finally:
                self._rollback_only = False
        return False


def _additive_upgrade(con: sqlite3.Connection) -> None:
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    duplicate = con.execute(
        """
        SELECT subscription_id, term_version, kind
        FROM reminder_deliveries
        GROUP BY subscription_id, term_version, kind
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate:
        raise RuntimeError("cannot migrate duplicate reminder deliveries")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_subscription_term_kind "
        "ON reminder_deliveries(subscription_id,term_version,kind)"
    )

def _has_structural_constraints(con: sqlite3.Connection) -> bool:
    subscription_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='subscriptions'").fetchone()[0] or ""
    reminder_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reminder_deliveries'").fetchone()[0] or ""
    subscription_fks = {tuple(row)[2:5] for row in con.execute("PRAGMA foreign_key_list(subscriptions)")}
    reminder_fks = {tuple(row)[2:5] for row in con.execute("PRAGMA foreign_key_list(reminder_deliveries)")}
    return (
        ("subscriptions", "replaced_from_id", "id") in subscription_fks
        and ("subscriptions", "subscription_id", "id") in reminder_fks
        and "CHECK(category IN ('AI','SERVER','OTHER'))" in subscription_sql
        and "CHECK(status IN ('active','inactive'))" in subscription_sql
        and "CHECK(kind IN ('BEFORE_48H','BEFORE_24H'))" in reminder_sql
        and "CHECK(status IN ('pending','claimed','sent','retryable','unknown','cancelled','failed'))" in reminder_sql
    )

def _repair_structural_constraints(con: sqlite3.Connection) -> None:
    if _has_structural_constraints(con):
        return
    if con.execute("SELECT 1 FROM reminder_deliveries r LEFT JOIN subscriptions s ON s.id=r.subscription_id WHERE s.id IS NULL LIMIT 1").fetchone():
        raise RuntimeError("cannot repair invalid legacy database rows")
    if con.execute("SELECT 1 FROM subscriptions WHERE category NOT IN ('AI','SERVER','OTHER') OR status NOT IN ('active','inactive') LIMIT 1").fetchone():
        raise RuntimeError("cannot repair invalid legacy database rows")
    if con.execute("SELECT 1 FROM reminder_deliveries WHERE kind NOT IN ('BEFORE_48H','BEFORE_24H') OR status NOT IN ('pending','claimed','sent','retryable','unknown','cancelled','failed') LIMIT 1").fetchone():
        raise RuntimeError("cannot repair invalid legacy database rows")
    sub_cols = {row[1] for row in con.execute("PRAGMA table_info(subscriptions)")}
    rem_cols = {row[1] for row in con.execute("PRAGMA table_info(reminder_deliveries)")}
    def col(columns, name, fallback):
        return name if name in columns else fallback
    try:
        con.execute("DROP TABLE IF EXISTS subscriptions_new")
        con.execute("DROP TABLE IF EXISTS reminder_deliveries_new")
        con.execute("""CREATE TABLE subscriptions_new (
            id TEXT PRIMARY KEY, owner_telegram_id INTEGER NOT NULL, name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 160),
            category TEXT NOT NULL CHECK(category IN ('AI','SERVER','OTHER')), status TEXT NOT NULL CHECK(status IN ('active','inactive')),
            end_at_utc TEXT NOT NULL, recurrence_value INTEGER, recurrence_unit TEXT CHECK(recurrence_unit IN ('DAYS','MONTHS','YEARS','NONE')),
            cost_minor INTEGER, currency TEXT, service_url TEXT, note TEXT, secret_ciphertext BLOB, secret_nonce BLOB,
            encryption_key_version INTEGER, term_version INTEGER NOT NULL DEFAULT 1, record_version INTEGER NOT NULL DEFAULT 1,
            replaced_from_id TEXT REFERENCES subscriptions_new(id), deactivation_reason TEXT, created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL, deactivated_at_utc TEXT
        )""")
        sub_names = "id,owner_telegram_id,name,category,status,end_at_utc,recurrence_value,recurrence_unit,cost_minor,currency,service_url,note,secret_ciphertext,secret_nonce,encryption_key_version,term_version,record_version,replaced_from_id,deactivation_reason,created_at_utc,updated_at_utc,deactivated_at_utc"
        sub_expr = ",".join([col(sub_cols, n, f) for n, f in (
            ("id", "NULL"), ("owner_telegram_id", "NULL"), ("name", "''"), ("category", "'OTHER'"), ("status", "'active'"), ("end_at_utc", "''"),
            ("recurrence_value", "NULL"), ("recurrence_unit", "'NONE'"), ("cost_minor", "NULL"), ("currency", "NULL"), ("service_url", "NULL"), ("note", "NULL"),
            ("secret_ciphertext", "NULL"), ("secret_nonce", "NULL"), ("encryption_key_version", "NULL"), ("term_version", "1"), ("record_version", "1"),
            ("replaced_from_id", "NULL"), ("deactivation_reason", "NULL"), ("created_at_utc", "''"), ("updated_at_utc", "''"), ("deactivated_at_utc", "NULL"))])
        con.execute(f"INSERT INTO subscriptions_new({sub_names}) SELECT {sub_expr} FROM subscriptions")
        con.execute("""CREATE TABLE reminder_deliveries_new (
            id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions_new(id) ON DELETE CASCADE, term_version INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('BEFORE_48H','BEFORE_24H')), scheduled_at_utc TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','claimed','sent','retryable','unknown','cancelled','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0, claimed_until_utc TEXT, sent_at_utc TEXT, telegram_message_id INTEGER,
            last_error_code TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
            UNIQUE(subscription_id,term_version,kind)
        )""")
        rem_names = "id,subscription_id,term_version,kind,scheduled_at_utc,status,attempt_count,claimed_until_utc,sent_at_utc,telegram_message_id,last_error_code,created_at_utc,updated_at_utc"
        rem_expr = ",".join([col(rem_cols, n, f) for n, f in (
            ("id", "NULL"), ("subscription_id", "NULL"), ("term_version", "1"), ("kind", "'BEFORE_24H'"), ("scheduled_at_utc", "''"),
            ("status", "'pending'"), ("attempt_count", "0"), ("claimed_until_utc", "NULL"), ("sent_at_utc", "NULL"), ("telegram_message_id", "NULL"),
            ("last_error_code", "NULL"), ("created_at_utc", "''"), ("updated_at_utc", "''"))])
        con.execute(f"INSERT INTO reminder_deliveries_new({rem_names}) SELECT {rem_expr} FROM reminder_deliveries")
        con.execute("DROP TABLE reminder_deliveries")
        con.execute("DROP TABLE subscriptions")
        con.execute("ALTER TABLE subscriptions_new RENAME TO subscriptions")
        con.execute("ALTER TABLE reminder_deliveries_new RENAME TO reminder_deliveries")
    except Exception:
        raise

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path, timeout=5, isolation_level=None, factory=TransactionalConnection)
    con.row_factory=sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA busy_timeout=5000"); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA secure_delete=ON")
    con.executescript(SCHEMA_SQL)
    current_version = con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        con.close()
        raise RuntimeError(f"database schema {current_version} is newer than application {SCHEMA_VERSION}")
    with con:
        _additive_upgrade(con)
        _repair_structural_constraints(con)
        if current_version < SCHEMA_VERSION:
            con.execute("INSERT INTO schema_migrations(version, applied_at_utc) VALUES (?, datetime('now'))", (SCHEMA_VERSION,))
    return con

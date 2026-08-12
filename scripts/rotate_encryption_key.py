from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.crypto import EncryptionService, load_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline AES-GCM key rotation")
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--old-key", required=True, type=Path)
    parser.add_argument("--new-key", required=True, type=Path)
    parser.add_argument("--new-version", required=True, type=int)
    args = parser.parse_args()
    if args.new_version <= 0:
        raise SystemExit("new version must be positive")
    old, new = EncryptionService(load_key(args.old_key)), EncryptionService(load_key(args.new_key), args.new_version)
    con = sqlite3.connect(args.db_path)
    con.row_factory = sqlite3.Row
    changed = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT id,secret_ciphertext,secret_nonce,encryption_key_version FROM subscriptions WHERE secret_ciphertext IS NOT NULL").fetchall()
        for row in rows:
            aad = f"subscription:{row['id']}:v1".encode()
            payload = old.decrypt(row["secret_ciphertext"], row["secret_nonce"], aad)
            ciphertext, nonce = new.encrypt(payload, aad)
            if new.decrypt(ciphertext, nonce, aad) != payload:
                raise RuntimeError("post-write decrypt verification failed")
            con.execute("UPDATE subscriptions SET secret_ciphertext=?,secret_nonce=?,encryption_key_version=?,updated_at_utc=? WHERE id=?", (ciphertext, nonce, args.new_version, datetime.now(timezone.utc).isoformat(), row["id"]))
            changed += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"rotated_records={changed}")


if __name__ == "__main__":
    main()

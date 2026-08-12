import base64
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db.connection import connect
from app.domain.crypto import EncryptionService
from app.repositories.subscriptions import SubscriptionRepository


class OfflineOperationsTests(unittest.TestCase):
    def test_backup_restore_and_key_rotation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            database, backup, restored = root / "source.db", root / "backup.db", root / "restored.db"
            old_key, new_key = os.urandom(32), os.urandom(32)
            old_path, new_path = root / "old.key", root / "new.key"
            old_path.write_text(base64.urlsafe_b64encode(old_key).decode())
            new_path.write_text(base64.urlsafe_b64encode(new_key).decode())
            con = connect(database)
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            encrypted = EncryptionService(old_key).encrypt({"login": "u", "password": "p", "api_key": None}, b"subscription:pending:v1")
            subscription_id = SubscriptionRepository(con).create(owner_id=1, name="X", category="AI", end_at=now + timedelta(days=3), recurrence=None, secret_payload=(encrypted[0], encrypted[1], 1), now=now)
            # Bind ciphertext to real UUID, as production service does.
            cipher, nonce = EncryptionService(old_key).encrypt({"login": "u", "password": "p", "api_key": None}, f"subscription:{subscription_id}:v1".encode())
            con.execute("UPDATE subscriptions SET secret_ciphertext=?,secret_nonce=? WHERE id=?", (cipher, nonce, subscription_id)); con.close()
            root_dir = Path("/app") if Path("/app/scripts/backup.py").exists() else Path(__file__).resolve().parents[1]
            subprocess.run([sys.executable, str(root_dir / "scripts/backup.py"), str(database), str(backup)], check=True)
            subprocess.run([sys.executable, str(root_dir / "scripts/restore.py"), str(backup), str(restored)], check=True)
            subprocess.run([sys.executable, str(root_dir / "scripts/rotate_encryption_key.py"), str(restored), "--old-key", str(old_path), "--new-key", str(new_path), "--new-version", "2"], check=True)
            checked = sqlite3.connect(restored).execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(checked, "ok")
            row = sqlite3.connect(restored).execute("SELECT secret_ciphertext,secret_nonce,encryption_key_version FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            self.assertEqual(row[2], 2)
            self.assertEqual(EncryptionService(new_key, 2).decrypt(row[0], row[1], f"subscription:{subscription_id}:v1".encode())["password"], "p")


if __name__ == "__main__":
    unittest.main()

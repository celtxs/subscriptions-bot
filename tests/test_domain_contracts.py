import unittest
from datetime import datetime, timezone

from app.domain.calendar import Recurrence, add_recurrence, moscow_to_utc
from app.domain.crypto import EncryptionService


class CalendarContractTests(unittest.TestCase):
    def test_month_end_uses_last_valid_day(self) -> None:
        value = datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            add_recurrence(value, Recurrence(1, "MONTHS")),
            datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc),
        )

    def test_leap_day_plus_year_uses_february_28(self) -> None:
        value = datetime(2024, 2, 29, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            add_recurrence(value, Recurrence(1, "YEARS")),
            datetime(2025, 2, 28, 9, 0, tzinfo=timezone.utc),
        )

    def test_moscow_input_converts_to_utc(self) -> None:
        self.assertEqual(
            moscow_to_utc("2026-02-01", "15:30"),
            datetime(2026, 2, 1, 12, 30, tzinfo=timezone.utc),
        )


class EncryptionContractTests(unittest.TestCase):
    def test_encrypt_decrypt_round_trip_and_tamper_fails_closed(self) -> None:
        service = EncryptionService(b"a" * 32, key_version=1)
        ciphertext, nonce = service.encrypt(
            {"login": "user", "password": "secret", "api_key": None},
            aad=b"subscription:abc:v1",
        )
        self.assertNotIn(b"secret", ciphertext)
        self.assertEqual(
            service.decrypt(ciphertext, nonce, aad=b"subscription:abc:v1"),
            {"login": "user", "password": "secret", "api_key": None},
        )
        with self.assertRaises(ValueError):
            service.decrypt(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), nonce, aad=b"subscription:abc:v1")


if __name__ == "__main__":
    unittest.main()

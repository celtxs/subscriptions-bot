from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptionService:
    def __init__(self, key: bytes, key_version: int = 1) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 key required")
        self._aes, self.key_version = AESGCM(key), key_version

    def encrypt(self, payload: dict[str, str | None], aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._aes.encrypt(nonce, encoded, aad), nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes, aad: bytes) -> dict[str, str | None]:
        try:
            decoded = self._aes.decrypt(nonce, ciphertext, aad)
            payload = json.loads(decoded)
        except Exception as exc:
            raise ValueError("secret payload cannot be decrypted") from exc
        if not isinstance(payload, dict) or set(payload) - {"login", "password", "api_key"}:
            raise ValueError("secret payload schema invalid")
        return {key: payload.get(key) for key in ("login", "password", "api_key")}


def load_key(path: Path) -> bytes:
    try:
        key = base64.urlsafe_b64decode(path.read_text(encoding="ascii").strip().encode("ascii"))
    except Exception as exc:
        raise RuntimeError("encryption key unavailable") from exc
    if len(key) != 32:
        raise RuntimeError("encryption key must decode to 32 bytes")
    return key

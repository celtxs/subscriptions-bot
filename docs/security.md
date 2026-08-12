# Security model

## Trust boundaries

- Telegram update is untrusted. Every router handler checks numeric `from_user.id` and `chat.type == private` before data access or mutation.
- Callback data carries only operation, UUID and optimistic version. It never carries a secret or user text.
- Secret plaintext is AES-256-GCM encrypted before persistence. AAD binds a payload to `subscription:<id>:v1`.
- `dialog_sessions` stores validated non-secret progress only. Login/password/API key are never written there.
- Logs use an allowlist of identifiers and event/error codes. No token, key, password, login, ciphertext, raw update, or message body belongs in logs.

## Limits

AES-GCM protects database secret fields only. A secret explicitly displayed in Telegram can remain in a client cache, notification, screenshot, or Telegram infrastructure. Best-effort deletion reduces exposure; it cannot guarantee erasure. Old encrypted database backups retain encrypted records until retention expiry.

## Incident response

If a token or encryption key is exposed, rotate it. If the master key is lost, ordinary subscription fields can be restored but encrypted secret fields cannot; re-enter those secrets. Never attempt recovery by weakening encryption or logging plaintext.

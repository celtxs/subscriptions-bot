# Backup and restore

Do not `cp` a live SQLite DB. Use `scripts/backup.py`, which uses SQLite Backup API then `integrity_check` and SHA-256.

The application container has no host backup mount. During operations, create the backup inside a controlled temporary application path, validate it, then copy it out to `/opt/subscriptions-bot/backups` using an operator command. Encrypt and transfer off-VPS separately. Keep master encryption-key backup separate from database backups. A DB backup without its corresponding key restores non-secret fields only.

Restore rehearsal: stop only `subscriptions-bot` after explicit approval; restore to a separate temporary volume; run integrity check, decrypt known test record, inspect subscription/reminder states, verify sent records are never made pending, then remove temporary volume. Never touch UAV.

## Rotation

Before key rotation: backup DB + checksum; retain old key; create new 32-byte random key in a new Docker secret; decrypt/re-encrypt each record within short transactions; verify each record decrypts under new key; record only IDs/counts/errors; create and validate new backup. Do not remove old key until restore rehearsal passes. The shipped rotation script refuses execution until this reviewed transactional implementation exists.

# Operations

## First launch gate

No launch occurred during development. Before first `up`, operator must create a **separate** BotFather token and set it only in `/opt/subscriptions-bot/.env`; create Base64-url encoded 32-byte key only in `/opt/subscriptions-bot/secrets/encryption.key`; set numeric owner ID. Do not paste any value into chat or logs.

Confirm no existing `subscriptions-bot` poller, then run only:

```bash
cd /opt/subscriptions-bot
docker compose -p subscriptions-bot config --quiet
docker compose -p subscriptions-bot up -d
docker compose -p subscriptions-bot ps
docker compose -p subscriptions-bot logs --tail=100
```

Expected external effect: one bot authentication and one polling process; no messages until an owner interacts or a reminder becomes due. Verify `getMe`/polling in logs without exposing token, owner-only `/start`, scheduler heartbeat, no published port, and unchanged UAV status.

## Update

1. Create and validate SQLite Backup API backup plus checksum.
2. Build tagged image; test/migrate a copy first.
3. Stop only `subscriptions-bot`; never run `down`, `restart`, or any command in UAV directory.
4. Start only `subscriptions-bot`; inspect health/logs, then smoke test.

## Rollback

1. Stop only `subscriptions-bot`.
2. Preserve logs and current DB backup.
3. Select prior image only if schema-compatible.
4. Start only `subscriptions-bot`; do not delete `subscriptions-data`.
5. Verify health, scheduler, reminder states. Restore DB only from a verified backup and matching key.

## Key rotation

Before rotation: create DB Backup API snapshot and checksum, retain old key, place new key outside Git, run script offline against stopped subscriptions-bot, then verify decrypt of test records and restore rehearsal. Never delete old key until this passes.

```bash
python scripts/rotate_encryption_key.py /path/to/subscriptions.db --old-key /secure/old.key --new-key /secure/new.key --new-version 2
```

Script reports count only. No plaintext is logged. Loss of master key means secret fields cannot be recovered; normal subscription fields remain recoverable.

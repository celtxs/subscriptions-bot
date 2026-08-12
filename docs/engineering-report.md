# Engineering report — offline completion status

## Scope and isolation

All work lives under `/opt/subscriptions-bot`. UAV code, Compose file, container, volume, token, `.env`, network and backup path were not read or changed. No subscriptions Compose service was launched.

## Architecture

```text
Telegram long polling (single aiogram process)
  ├─ owner/private authorization
  ├─ dialog router + persistent non-secret sessions
  ├─ subscription repository (SQLite transactions)
  ├─ AES-256-GCM secret storage
  └─ APScheduler
       └─ SQLite lease, expiry, durable reminder claims
```

## Compose resources

| Type | Value |
|---|---|
| Compose project | `subscriptions-bot` |
| Container | `subscriptions-bot` |
| Image | `subscriptions-bot:local` |
| Volume | `subscriptions-data` |
| Network | `subscriptions-bot_default` |
| DB in container | `/app/data/subscriptions.db` |
| Published ports | none |
| Docker socket | absent |

Limits: 256 MiB RAM hard / 128 MiB reservation / 384 MiB mem+swap, 0.25 CPU, PID 64, non-root UID/GID 10001, read-only root FS, dropped caps, no-new-privileges, `/tmp` 32 MiB tmpfs.

## Deliberate launch gate

Offline evidence does not prove Bot API identity, Telegram owner delivery, Compose health over time, restart/reboot, actual container resource use, or production restore rehearsal. Those remain a separately approved first-launch phase.

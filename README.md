# subscriptions-bot

Personal owner-only Telegram subscriptions tracker. Not launched by repository setup.

## Safety boundary

Separate Compose project `subscriptions-bot`; separate `subscriptions-data` volume and `subscriptions-bot_default` network. No ports, Docker socket, host network, privileged mode, UAV mounts, UAV network, volume, database, token, or project files.

Before launch: replace local `.env` placeholders with separate BotFather token and numeric owner ID; replace `secrets/encryption.key` with Base64-url encoded random 32-byte AES key. Both paths are gitignored.

Run only after explicit launch approval:

```bash
docker compose -p subscriptions-bot up -d
```

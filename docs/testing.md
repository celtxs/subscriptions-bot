# Test evidence

## Offline command

The canonical offline gate builds the image and runs the suite inside it:

```bash
docker build --no-cache -t subscriptions-bot:offline-release .
docker run --rm --network none subscriptions-bot:offline-release python -m unittest discover -s tests -v
```

The latest verified image gate ran **49 tests** and returned `OK`. The image test run has no application network access and uses temporary SQLite files only.

Coverage includes calendar boundaries, AES-GCM/tamper fail-closed behavior, owner/private authorization, expiring non-secret sessions, atomic lifecycle transactions and rollback, stale callbacks, reminder claim/lease/retry/ambiguous outcomes, scheduler heartbeat, UI add/edit/replacement/secret-display flows, legacy-schema migration and fail-closed future/duplicate states, backup/restore/key rotation, and logging allowlists.

Additional offline checks:

```bash
docker compose -p subscriptions-bot -f compose.yaml config --quiet
docker run --rm --network none subscriptions-bot:offline-release pip check
git diff --check
```

Do not run `docker compose up`, polling, Telegram API calls, webhook setup, or message deletion as part of this offline gate.

Not verified offline: real Bot API acceptance, polling lifecycle, Docker health after Compose launch, actual resource use, reboot, and restore rehearsal against a production volume. Those require a separate first-launch approval.

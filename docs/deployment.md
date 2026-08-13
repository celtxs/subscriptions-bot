# Deployment gate

Repository preparation does not start a container or contact Telegram. Before first start obtain explicit approval and confirm: separate token present (not shown), numeric owner ID, valid AES key, `docker compose -p subscriptions-bot config --quiet`, no competing poller, and backup/recovery plan.

Resource profile: 256 MiB hard memory, 128 MiB reservation, 384 MiB memory+swap, 0.25 CPU, PID 64, tmpfs `/tmp` 32 MiB. No published ports. Because this Docker Compose backend ignores file-secret ownership options, the encryption key is bind-mounted read-only with host mode `0640`; the process remains UID `10001` and receives only the host secret's supplemental group for read access. The key remains outside the image and version control.

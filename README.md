# ASMarr

ASMarr monitors ASMR creators, downloads audio and optional videos, organizes
their libraries, and updates Plex. It runs on Linux with a single local
administrator. Direct sources are preferred; Prowlarr/qBittorrent provide audio
fallback and interactive video acquisition.

## Start here

- [Install ASMarr](docs/install.md)
- [Daily use and troubleshooting](docs/operations.md)
- [Backups, full recovery and rollback](docs/recovery.md)
- [Passwords, sessions and HTTPS](docs/security.md)
- [Live testing](docs/live-testing.md)

The guides describe the hardened release; check your installed release identity
before using new operator commands. Deployment addresses and operational state
belong in private operator records, not public documentation.

## Everyday use

Add creators on **Creators**, link their source identities, select an acquisition
profile, and enable audio monitoring. **Monitor Videos** is a separate opt-in:
enabling it requests the creator's complete history available from supported
sources, not just future uploads. Review the candidate count and disk space first.

Use **Wanted**, **Queue**, **History**, and **Calendar** to follow recordings.
Video views show independent audio/video states. Failed work remains available
for review; do not repeatedly submit a command whose outcome is unconfirmed.

Select creator checkboxes to monitor, unmonitor, edit or remove several at once.
Removal defaults to **keep files** and **prevent automatic re-addition**. Deleting
files requires confirmation and affects only individually tracked safe paths.
Download-client originals and untracked files are retained.
[More detail](docs/operations.md#remove-creators).

## Settings and paths

Configure source policies, acquisition profiles, naming, integrations and media
roots in **Settings**. Source credentials stay in protected server configuration.
Passwords and API keys do not belong in Git, issue reports or screenshots.

| Item | Location |
| --- | --- |
| Application | `/opt/asmarr` |
| Protected configuration | `/etc/asmarr` |
| Database and runtime state | `/var/lib/asmarr` |
| Audio library | Your configured audio root |
| Video library | Your configured video root |
| qBittorrent downloads | Your configured download path; category `asmarr` |

Set the video path in **Settings → video root** and bind it to a dedicated Plex
**Other Videos** library. Audio requires its own Plex **Music** library.
Audio/video roots must not overlap. Default video limits are one transfer at a
time and a 20 GiB free-space reserve.

## First login and recovery

There is no shared default password. First startup generates a unique password.
Retrieve it from a private server terminal:

```sh
sudo python3 /opt/asmarr/ops/admin_login.py
```

For a source checkout, use `ops/admin_login.py` instead. Change the password in
Settings after login. Lost password? Use the same helper with `--reset`; it
generates a new password and displays it only in an interactive terminal.
Password changes/resets revoke browser sessions; API keys remain unchanged.
[Security details](docs/security.md).

## Backups and updates

The hardened backup task creates encrypted recovery bundles containing SQLite,
protected configuration, the application, and its service configuration.
**Media is not included.** Set up the recovery recipient and off-host pull before
relying on these bundles. Keep the recovery private key separately: without it,
encrypted backups cannot be restored. [Setup and recovery](docs/recovery.md).

Updates are explicit, verified operations with a rollback backup. Unattended
application updates are not enabled. Never run the shadow-only deployment tool
on an existing production installation.

## Development and advanced reference

The backend uses ASP.NET Core .NET 10, SQLite/WAL, OpenAPI and SignalR.
The frontend uses React, TypeScript and Vite. Providers use Python, ffprobe,
yt-dlp and Deno. REST resources are under `/api/v1`; authenticated OpenAPI is
at `/openapi/v1.json`. API clients send `X-Api-Key`.

```sh
cd frontend
npm ci
npm run build
cd ..
dotnet publish ASMarr/ASMarr.csproj -c Release -r linux-x64 --self-contained true -o publish
python3 -m unittest discover -s tests -v
dotnet run --project tests/StoreTests/StoreTests.csproj -c Release
cd tests/browser
npm ci
npx playwright install chromium
npm test
```

Tests use isolated databases and recorded/local fixtures, not production
credentials. [Historical migration, acceptance gates and audited deployment
details](docs/rollout-audit.md) are separate from the normal user workflow.

Jellyfin/Emby, SABnzbd/Usenet, multi-user accounts, import lists, custom formats,
and unattended updates remain outside the current release.

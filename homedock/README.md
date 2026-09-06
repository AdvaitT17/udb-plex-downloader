# Running on HomeDock OS

HomeDock OS installs apps from `.hds` packages: a docker-compose file plus an
icon and metadata, bundled by the built-in **Packager** app and then installed
from the App Store like any official app. This directory contains the compose
file for that package. Nothing else from `scripts/` is needed — `install.sh`
and `backup.sh` are Umbrel-specific (HomeDock persists the app's AppData
across OS updates by itself).

## 1. Get the image

The compose references `ghcr.io/advaitt17/udb-plex-downloader:latest`, built
automatically for amd64 + arm64 by the `docker-publish` GitHub Actions
workflow on every push to `main`. If you fork this repo, update the image name
in [docker-compose.yml](docker-compose.yml) to your GHCR path and make the
package public (GitHub → Packages → package settings → Change visibility).

## 2. Check the library mount

The volume line

```yaml
- "[[APP_MOUNT_POINT]]/plex/tvseries:/downloads"
```

matches HomeDock's **official Plex app**, which mounts
`[[APP_MOUNT_POINT]]/plex/tvseries` at `/tv` — its TV library. If you
installed Plex that way, downloads land in the library with no changes.

If your Plex compose mounts a different host path for TV (check
**Control Hub → Plex** → compose/mounts), edit the line to that path — either
before packaging, or later in Packager's built-in compose editor.

## 3. Build the `.hds` package

1. Open the **Packager** app in HomeDock OS → **Package Generator**.
2. Upload [homedock/docker-compose.yml](docker-compose.yml).
3. Upload an icon (`.png`/`.jpg`, ideally 512×512 — `extension/icons/icon128.png`
   works in a pinch).
4. Fill the metadata:

   | Field        | Value                                        |
   |--------------|----------------------------------------------|
   | App Slug     | `udb-plex-downloader`                        |
   | App Name     | UDB Plex Downloader                          |
   | Category     | Media                                        |
   | Type         | Download Automation                          |
   | Docker Image | `ghcr.io/advaitt17/udb-plex-downloader`      |
   | Version      | `latest`                                     |
   | Author       | AdvaitT17                                    |
   | Description  | One-click KissKh → Plex episode downloader with auto-rescan of ongoing shows |

5. Click **Create & Download .hds Package**, then import it in
   **Package Manager** (drag & drop). The app now appears in your App Store.
6. Install it from the App Store.

## 4. Grab the auth token for the Chrome extension

`UDB_TRIGGER_TOKEN` is set to the `[[HD_RND_STR]]` DevHook, so HomeDock
generates a random 16-character token at install time. Read the rendered value
in **Control Hub → udb-plex-downloader** (compose/environment view), then put
it in the extension's options page along with the trigger URL
`http://<homedock-ip>:8787`.

Prefer a token you choose yourself? Replace `[[HD_RND_STR]]` with your own
value in Packager's compose editor before installing.

## 5. Verify

```bash
curl http://<homedock-ip>:8787/health
# {"ok":true,"token_configured":true}
```

Dashboard: `http://<homedock-ip>:8787/dashboard`.

## Notes

- **Persistence**: the job queue and logs live in
  `[[INSTALL_PATH]]/udb-plex-downloader/data` (e.g.
  `/DATA/HomeDock/AppData/udb-plex-downloader/data` on Linux), so they survive
  container rebuilds and app updates. The Umbrel backup/restore scripts are
  not needed.
- **Updates**: push to `main` → CI publishes a new `latest` image → reinstall/
  update the app from HomeDock (or `docker compose pull` equivalent via
  Control Hub restart).
- **Security**: LAN-only, shared-secret auth, no TLS — same caveats as the
  Umbrel deployment. Don't port-forward 8787 to the internet.
- **Plex refresh**: to auto-refresh the Plex library after each download,
  uncomment `UDB_PLEX_REFRESH_URL` in the compose and fill in your section id
  and Plex token.

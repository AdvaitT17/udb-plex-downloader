# udb-plex-downloader

One-click KissKh → Umbrel → Plex pipeline. Click a button on a KissKh show page in Chrome, the episodes download directly onto your Umbrel server into the Plex library, and new episodes of ongoing shows auto-download daily.

Built on top of [Prudhvi-pln/udb](https://github.com/Prudhvi-pln/udb) (the `udb.py` CLI downloader) with a FastAPI trigger service, a Chrome MV3 extension, and install/backup scripts tuned for Umbrel OS.

## How it works

```
  Chrome (Mac)                  Umbrel host                  Plex
  ┌──────────────┐   HTTP    ┌──────────────────┐  writes  ┌──────────┐
  │ Extension    │ ────────▶ │ trigger service  │ ───────▶ │ library  │
  │ "Download to │  POST     │ (FastAPI +       │  mp4s    │ /tvshows │
  │  Plex"       │  /jobs    │  sqlite + udb.py)│          └──────────┘
  └──────────────┘           └──────────────────┘
                                       │
                                       │ daily rescan
                                       ▼
                              ongoing shows pick up
                              new episodes automatically
```

## Components

- **`trigger/`** — FastAPI service + SQLite job queue + embedded live dashboard at `http://umbrel.local:8787/dashboard`. Renames downloaded files to Plex-friendly `Show - s01eNN.mp4`, tracks "watches" for ongoing series, re-scans daily via APScheduler.
- **`extension/`** — Chrome MV3 extension. Injects a *Download to Plex* button on KissKh pages, scrapes title/year/ongoing status, posts to the trigger. Notifications click-open the dashboard. Popup lists watches + Rescan now.
- **`scripts/install.sh`** — one-shot bootstrap for a freshly-wiped Umbrel box. Clones the repo, restores the latest backup (queue + token) from the Plex-survival directory, writes `.env`, builds + starts the container, polls `/health`.
- **`scripts/backup.sh`** — snapshots `.env` + `queue.sqlite` + recent logs to `~/umbrel/home/Downloads/udb-backups/` (survives Umbrel OS updates). `--cron` installs a systemd **user timer** (Umbrel OS has no cron) with `loginctl enable-linger` so it keeps firing after logout.
- **`udb.py`, `Clients/`** — upstream CLI with small patches: `shutil.get_terminal_size()` fallback for headless runs, `curl_cffi` chrome124 impersonation + `UDB_THROTTLE_SECONDS` for Cloudflare.

## Quick start

On the Umbrel box:

```bash
curl -fsSL https://raw.githubusercontent.com/AdvaitT17/udb-plex-downloader/main/scripts/install.sh | bash
bash ~/udb/scripts/backup.sh --cron   # daily 03:30 snapshots
```

The installer prints the auth token — paste it into `extension/background.js` (`UDB_TRIGGER_TOKEN`) and load `extension/` as an unpacked extension in Chrome.

## Dashboard

`http://umbrel.local:8787/dashboard` — live job list with status badges, per-job Cancel / Delete / Log controls, one-click cleanup of finished jobs, and a "watches" panel for ongoing shows you want auto-updated.

## Environment

- `UDB_TRIGGER_TOKEN` — shared secret between extension and server (kept inline in `background.js`; rotated via `.env` on the host).
- `UDB_RESCAN_AT` — daily rescan time, default `04:15`.
- `TZ` — defaults to `Asia/Kolkata`.
- `UDB_THROTTLE_SECONDS` — optional per-request delay in the Cloudflare-bypass client.

## Disaster recovery

Umbrel OS updates wipe Docker state and `/home/umbrel` customizations but preserve `~/umbrel/home/Downloads/`. Snapshots live there, so re-running `install.sh` after a wipe restores the queue + auth token and the Chrome extension keeps working without changes.

## Credit

Core downloader: [Prudhvi-pln/udb](https://github.com/Prudhvi-pln/udb). Everything under `trigger/`, `extension/`, `scripts/`, and `docker-compose.yml` is this repo's addition.

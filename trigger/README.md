# UDB → Plex trigger

Automates the flow:

```
Mac (Chrome on kisskh.*)
  └─ click "⬇ Download to Plex"
       └─ extension POST → Umbrel trigger service (FastAPI)
            └─ queues + runs `python udb.py ...` serially
                 └─ output lands in Plex TV library
```

No more manual `scp`. Files land directly in `~/umbrel/home/Downloads/tvshows/`.

## Pieces

- `trigger/` — FastAPI + SQLite job queue that wraps `udb.py`
- `trigger/Dockerfile` + `docker-compose.yml` — containerised for Umbrel
- `extension/` — Chrome MV3 extension that injects a button on kisskh show pages

## 1. Install the trigger service on Umbrel

SSH into Umbrel, clone this repo, then:

```bash
cd udb
export UDB_TRIGGER_TOKEN=$(openssl rand -hex 16)
echo "Token (save this for the extension): $UDB_TRIGGER_TOKEN"
docker compose up -d --build
```

Verify:

```bash
curl http://<umbrel-lan-ip>:8787/health
# {"ok":true,"token_configured":true}
```

Default download path (mounted into the container at `/downloads`) is
`~/umbrel/home/Downloads/tvshows/`. Edit the `volumes:` block in
`docker-compose.yml` if your Plex library lives elsewhere.

### Optional: auto-refresh Plex after a job finishes

Set `UDB_PLEX_REFRESH_URL` in `docker-compose.yml` to your Plex section refresh
URL:

```
UDB_PLEX_REFRESH_URL: "http://<plex-host>:32400/library/sections/<id>/refresh?X-Plex-Token=<token>"
```

## 2. Install the Chrome extension on your Mac

1. Open `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked** → select the `extension/` folder in this repo
4. Click the extension's **Details** → **Extension options**, and set:
   - **Umbrel trigger URL** — e.g. `http://192.168.1.50:8787` or `http://umbrel.local:8787`
   - **Auth token** — the `UDB_TRIGGER_TOKEN` you generated above
5. Click **Test connection** — should report `token_configured=true`

## 3. Use it

1. Browse to any show on kisskh (e.g. `/Drama/<show-name>`)
2. A red **⬇ Download to Plex** button appears under the title
3. Click it → inline status shows `✓ queued (job #N)` and you get a Chrome notification
4. Click the toolbar icon to see recent jobs / statuses
5. When the job finishes, files are in `~/umbrel/home/Downloads/tvshows/<Show Name>/`

## API (if you want to script it)

All endpoints except `/health` require header `X-Auth-Token: <token>`.

```bash
# queue a show by name
curl -X POST http://umbrel.local:8787/download \
  -H "X-Auth-Token: $UDB_TRIGGER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Reborn Rich","year":2022,"series_type":2}'

# list jobs
curl http://umbrel.local:8787/jobs -H "X-Auth-Token: $UDB_TRIGGER_TOKEN"

# view one job incl. last 50 log lines
curl http://umbrel.local:8787/jobs/3 -H "X-Auth-Token: $UDB_TRIGGER_TOKEN"
```

Request fields:

| field         | type       | default | notes                                         |
|---------------|------------|---------|-----------------------------------------------|
| `name`        | string     | —       | required; passed to `udb.py -n`               |
| `year`        | int        | null    | disambiguates search                          |
| `series_type` | int        | `2`     | UDB client index; `2` = KissKh                |
| `seasons`     | list[str]  | null    | repeats `-S`                                  |
| `episodes`    | list[str]  | null    | e.g. `["1-12"]` or `["3","5","7-9"]`          |
| `resolution`  | string     | null    | e.g. `"720"`; UDB picks one if omitted        |

## Troubleshooting

- **Button doesn't appear on kisskh** — the site is a React SPA; the injector
  watches DOM mutations and waits for an `<h1>`. Scroll/refresh if it's slow.
- **Button says `✗ HTTP 401`** — token mismatch; re-check the options page.
- **Job status stays `failed`** — fetch `/jobs/{id}` to see the UDB log tail.
  Common causes: show name didn't match search, ffmpeg missing (shouldn't
  happen in the container), network.
- **Permission denied writing to `/downloads`** — the Umbrel host directory is
  owned by a non-root user. Either `chmod 777` the mount, or add
  `user: "1000:1000"` under the service in `docker-compose.yml` (matching the
  uid that owns `tvshows/`).

## Security note

The service has no TLS and only a shared-secret token — fine for LAN-only use,
do **not** expose port 8787 to the internet. If you want remote access, put it
behind Tailscale or a reverse proxy with TLS.

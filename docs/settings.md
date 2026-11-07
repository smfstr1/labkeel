# Settings

LabKeel is set up in two places:

- **The dashboard itself**, under **Settings**: alerts, restart schedules, container update checks, drive care
  and so on. Changes take effect straight away.
- **`docker-compose.yml`**, for the things that shouldn't be changeable from a web page: what LabKeel is allowed
  to do to the machine, how it's reached, and where it keeps its data. Change a line, then run
  `docker compose up -d` to apply it. When LabKeel updates, it adds any new settings here for you, switched
  off ([updates.md](updates.md#your-docker-composeyml)).

This page lists every `docker-compose.yml` setting. They go under `environment:`, one per line:

```yaml
    environment:
      - LABKEEL_ALLOW_FILE_CHANGES=1
```

On/off settings take `1`, `true`, `yes` or `on` (and `0`, `false`, `no` or `off`). If a value can't be read,
LabKeel says so in its log (`docker logs labkeel`) and uses the default.

## What LabKeel may change

All off by default. Each one is a real power over the machine, so turn on only what you use.

| Setting | Default | What it allows |
| --- | --- | --- |
| `LABKEEL_ALLOW_FILE_CHANGES` | `0` | Uploading, editing, renaming, moving and deleting files in the file browser. Folders mounted `:ro` stay read-only either way. |
| `LABKEEL_ALLOW_ZFS_CHANGES` | `0` | Creating, changing and destroying ZFS pools, datasets and snapshots, and replacing drives. |
| `LABKEEL_ALLOW_RESTART` | `0` | Restarting and shutting down the machine, by hand or on a schedule. |
| `LABKEEL_ALLOW_APP_INSTALLS` | `0` | Adding and removing apps on the Apps page. An app's containers get whatever its compose file asks for, and a few (Home Assistant, Plex) need the machine's network or full control of it. See [apps.md](apps.md). |

## Sign-in

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_ADMIN_USER` | | An admin account set here rather than on the set-up page. Needs `LABKEEL_ADMIN_PASSWORD_HASH`. |
| `LABKEEL_ADMIN_PASSWORD_HASH` | | That account's password, as an Argon2id hash: `docker exec -it labkeel python accounts.py hash`. In `docker-compose.yml`, write each `$` as `$$`. |
| `LABKEEL_ALLOW_NO_SIGN_IN` | `0` | `1` opens the dashboard to anyone who can reach it, with no accounts. Only for a machine nobody else can reach. |
| `LABKEEL_HOSTNAMES` | | With sign-in off, the extra names (comma-separated) the dashboard answers to, besides the machine's own. |

## HTTPS and the network

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_PORT` | `8787` | The port to open in your browser. |
| `LABKEEL_HTTPS` | `auto` | `auto` uses your own certificate if there's one in the certs folder (`tls.crt` and `tls.key`, or `fullchain.pem` and `privkey.pem`), else a Tailscale one if it can get one, else LabKeel's own self-signed one. `tailscale` or `self-signed` force one. `off` is plain HTTP, for use behind your own HTTPS reverse proxy (set `LABKEEL_TRUSTED_PROXY` too: LabKeel only signs people in over HTTPS). |
| `LABKEEL_HSTS` | `0` | `1` tells browsers to always use `https://` for this name. Only if nothing else on the same name uses plain `http://`, on any port. |
| `LABKEEL_LISTEN_ADDRESS` | `0.0.0.0` | The address to listen on. `127.0.0.1` keeps it to this machine (behind a reverse proxy on the same machine, say). |
| `LABKEEL_MAX_CONNECTIONS` | `512` | The most connections open at once. |

## Behind a reverse proxy

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_TRUSTED_PROXY` | | Your proxy's address, or several, or networks (`127.0.0.1, 10.0.0.0/24`). Only requests from these may say who the visitor is (`X-Forwarded-For`) and that they came over HTTPS (`X-Forwarded-Proto`). |
| `LABKEEL_PROXY_USER_HEADER` | | Let the proxy sign people in (Authelia, Authentik, oauth2-proxy): the header it puts the user name in, such as `Remote-User`. The proxy must set it itself and remove any copy the visitor sends. |
| `LABKEEL_PROXY_ADMINS` | | Names the proxy signs in who are admins, comma-separated. Names that have a LabKeel account use that account's role. |
| `LABKEEL_PROXY_VIEWERS` | | Names the proxy signs in who may look but not change anything. `*` means everyone the proxy signs in. |

## Files

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_UPLOAD_LIMIT_GB` | no limit | The largest single upload, in GB. |
| `LABKEEL_BIN_DAYS` | `30` | Days deleted files stay in the Bin before they're gone for good. |

## Apps

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_APPS_DIR` | `/srv/labkeel/apps` | Where each app you add gets its own folder on the machine: its compose file and its settings. A path on the machine, not inside LabKeel's container. |

## The agent, on your other machines

These go in `agent.compose.yml` on the other machine, not in LabKeel's own `docker-compose.yml`. The **Add a
server** dialog fills them in for you. See [servers.md](servers.md).

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_HUB` | | Your main LabKeel's address, such as `https://homelab:8787`. Must be `https://`. |
| `LABKEEL_JOIN_CODE` | | The one-time code from **Add a server** (the arrow beside the machine's name). Works once, for 15 minutes. Not needed once the machine has joined. |
| `LABKEEL_HUB_KEY` | | Your main LabKeel's certificate key (`sha256:…`), shown beside the code. With it the agent knows it's talking to your LabKeel, even with a self-signed certificate. |

## Rarely needed

The example `docker-compose.yml` already matches these, so leave them alone unless you've changed its folders.

| Setting | Default | What it does |
| --- | --- | --- |
| `LABKEEL_DATA_DIR` | `/data` | Where LabKeel keeps accounts, settings, history and the activity log, inside the container. |
| `LABKEEL_CONFIG_FILE` | `/config/config.json` | The services file ([services.md](services.md)). |
| `LABKEEL_CERTS_DIR` | `/certs` | HTTPS certificates: LabKeel's own, or yours (see `LABKEEL_HTTPS`). |
| `LABKEEL_FILES_DIR` | `/browse` | The folder whose sub-folders appear in the file browser. |
| `LABKEEL_HOST_COMMANDS` | `1` | Run disk, ZFS and Docker Compose commands on the machine itself (needs `pid: host` and `privileged: true`). `0` keeps LabKeel inside its container: it can still watch, but not update containers or itself. |
| `LABKEEL_LOG_LEVEL` | `warning` | How much the web server writes to the log: `error`, `warning`, `info` or `debug`. |
| `LABKEEL_DRIVE_CHECK_SECONDS` | `600` | Seconds between drive health (SMART) readings. |
| `LABKEEL_POOL_CHECK_SECONDS` | `20` | Seconds between quick looks at the ZFS pools, for failed drives. |
| `LABKEEL_POOL_FULL_CHECK_SECONDS` | `120` | Seconds between full looks at the pools while everything's well. |
| `LABKEEL_ZFS_CHECK_SECONDS` | `60` | Seconds between checks for scheduled snapshots, scrubs and TRIMs. |
| `LABKEEL_POWERCAP_DIR` | `/sys/class/powercap` | Where the CPU's power readings are. |
| `TZ` | the machine's | The time zone, only if the machine has no `/etc/timezone`. Used for schedules. |

## Names from before 0.1.2

Settings used to start with `DASH_`, and some had other names. The old names still work, and LabKeel lists
any it finds in its log with their new names, so you can rename them when convenient:

| Old name | New name |
| --- | --- |
| `DASH_FILES_WRITE` | `LABKEEL_ALLOW_FILE_CHANGES` |
| `DASH_ZFS_WRITE` | `LABKEEL_ALLOW_ZFS_CHANGES` |
| `DASH_ALLOW_REBOOT` | `LABKEEL_ALLOW_RESTART` |
| `DASH_USER` | `LABKEEL_ADMIN_USER` |
| `DASH_PASS_HASH` | `LABKEEL_ADMIN_PASSWORD_HASH` |
| `DASH_ALLOW_NO_AUTH` | `LABKEEL_ALLOW_NO_SIGN_IN` |
| `DASH_HOSTNAMES` | `LABKEEL_HOSTNAMES` |
| `DASH_PORT` | `LABKEEL_PORT` |
| `DASH_HOST` | `LABKEEL_LISTEN_ADDRESS` |
| `DASH_TLS` | `LABKEEL_HTTPS` |
| `DASH_HSTS` | `LABKEEL_HSTS` |
| `DASH_MAX_CONNECTIONS` | `LABKEEL_MAX_CONNECTIONS` |
| `DASH_TRUSTED_PROXY` | `LABKEEL_TRUSTED_PROXY` |
| `DASH_AUTH_HEADER` | `LABKEEL_PROXY_USER_HEADER` |
| `DASH_PROXY_ADMINS` | `LABKEEL_PROXY_ADMINS` |
| `DASH_PROXY_VIEWERS` | `LABKEEL_PROXY_VIEWERS` |
| `DASH_UPLOAD_MAX_GB` | `LABKEEL_UPLOAD_LIMIT_GB` |
| `DASH_TRASH_DAYS` | `LABKEEL_BIN_DAYS` |
| `DASH_DATA` | `LABKEEL_DATA_DIR` |
| `DASH_CONFIG` | `LABKEEL_CONFIG_FILE` |
| `DASH_CERTS` | `LABKEEL_CERTS_DIR` |
| `DASH_BROWSE_ROOT` | `LABKEEL_FILES_DIR` |
| `DASH_HOST_NS` | `LABKEEL_HOST_COMMANDS` |
| `DASH_LOG_LEVEL` | `LABKEEL_LOG_LEVEL` |
| `DASH_SMART_INTERVAL` | `LABKEEL_DRIVE_CHECK_SECONDS` |
| `DASH_DRIVECARE_TICK` | `LABKEEL_POOL_CHECK_SECONDS` |
| `DASH_DRIVECARE_FULL` | `LABKEEL_POOL_FULL_CHECK_SECONDS` |
| `DASH_ZFS_TICK` | `LABKEEL_ZFS_CHECK_SECONDS` |
| `DASH_POWERCAP` | `LABKEEL_POWERCAP_DIR` |

# The LabKeel guide

Everything LabKeel does, page by page, and how it keeps the machine safe while doing it. To install it, see
[INSTALL.md](../INSTALL.md); for every `docker-compose.yml` setting, see [settings.md](settings.md).

LabKeel is one container that shows your server at a glance and lets you look after it:

- **Overview**: a "needs attention" list at the top (failed drives, rebuilds, crashed containers, services down, updates waiting for a restart), live CPU (per thread, temperature, load), memory, GPU and network, then a History section (last hour, day, week or month) charting the server's power use and GPU load, plus service, container, storage and drive summaries. **Customise** chooses which cards show, and in what order. Select the CPU or memory card to see the busiest programs and which container each belongs to, or the GPU card for each GPU's power state, memory and the programs using it.
- **Containers**: every container grouped by Compose project, with status, health, uptime, CPU, memory, network and published ports (clickable). Select one to read, search or follow its logs, or to restart, stop, start, pause or remove it; tick several to act on them all at once. Restart and stop ask first and warn if players are connected. A container with a newer image out gets an **Update** badge, and one started by Docker Compose can be updated from the dashboard, with the old image put back if the new one doesn't start ([updates.md](updates.md)). Containers you stop here don't trigger crash or service-down alerts. The dashboard can restart itself but won't stop itself.
- **Storage**: a one-line health summary, any drive problems with what to do about them, then each ZFS pool (usable space, layout, last scrub, scrub controls) with its drives inside it, then your other drives with what's on them. A drive ZFS can't see any more stays on the page as an outline, with its name, serial and slot, so you know which one to pull. Each drive tile shows its status, temperature, wear and age. Select one for full details: SMART self-check, spare, media errors, sector counts, data written and read, power-on hours and cycles, model, serial and firmware, plus renaming. Mounted filesystems are listed below. Each pool has its own page for datasets, snapshots (including automatic ones), scrubs, TRIM and changing its layout, and you can create and import pools (see [Pools](#pools)).
- **Files**: a folder tree with previews for text, images, video and audio, downloads and folder sizes; in folders mounted read-write, uploads, new folders, renaming, moving, editing text and a 30-day Bin
- **Services**: links to everything you run, each with a live up/down check (HTTP, TCP, or a Minecraft Bedrock ping that shows players online), as logos, a list or detailed cards
- **Apps** (admins): a catalogue of popular self-hosted apps, or your own compose file. LabKeel writes the compose file into the app's own folder, starts it, waits until it answers and adds it to Services. Off until `LABKEEL_ALLOW_APP_INSTALLS=1` ([apps.md](apps.md)).
- **Servers**: your other machines (a NAS, a Raspberry Pi, a second server), each running LabKeel's small agent and reporting here over HTTPS, with their figures, containers, storage and drive health. The arrow beside the machine's name switches between them, and **Containers → All servers** lists every container on every machine. For now they're shown, not changed ([servers.md](servers.md)).
- **Settings**: alerts, restart schedules, update checks, drive care, accounts and LabKeel's own updates, each on its own page


## Services and names

1. **`config/config.json` → `services`** (the full reference is in [services.md](services.md)): `url` is the link your browser opens, so use the address you'd type into your browser (a Tailscale or reverse-proxy name, say). `check` is what the server probes, which is usually `127.0.0.1` plus the port. Check types:
   - `"http://…"`: up if it answers with anything below HTTP 500
   - `{ "type": "tcp", "host": "…", "port": 443 }`: up if the port accepts a connection
   - `{ "type": "bedrock", "host": "127.0.0.1", "port": 19132 }`: pings a Bedrock server and shows players online and version
   - Leave `check` out to probe `url` itself. Use `{ "type": "none" }` for link-only entries.

   Add `"container": "<container name>"` to tie a service to its container (the Compose service name works too). A stopped container then shows the service as down. A running container that doesn't answer shows amber, with the reason. For `bedrock` checks with a container, the ping goes straight to the container's own address and port, so no host or port is needed. Add a second Bedrock server by copying that entry with the other container's name.

   `{gateway}` in a `url` or `check` is replaced with your router's address, read from the server's routing table.

   **Display names:** rename any container (open it, then Rename) or service (pencil on its card) from the dashboard. Names are saved in `./data/overrides.json` and survive rebuilds. They only change what the dashboard shows.
2. **`config/config.json` → `drive_labels`** (optional): map a drive's serial (shown on its card on the Storage page) to a friendly name. Config changes apply without a restart.
3. **`docker-compose.yml` → volumes under `/browse`**: these are the folders that appear in the file browser. To browse a ZFS pool, add its mountpoint (`zfs get mountpoint <pool>`).

## GPUs, power and history

- **GPUs**: read from the host's `nvidia-smi`, which keeps running and reports every 5 seconds (at your live refresh rate while a dashboard page is open). The Overview shows average use, the hottest temperature, memory, and how many video encodes are running (Plex hardware transcoding shows up here). No NVIDIA driver? The card shows your containers instead.
- **Power**: the CPU and memory report their own energy use (RAPL), and each GPU reports its power draw. Drives, fans, the motherboard and the power supply's losses can't be measured from software, so Settings → Power estimate has a **Rest of the machine** allowance. With a smart plug, compare its reading with the Overview figure and enter the difference. Add your electricity price to see costs and a monthly estimate.
- **History**: kept in `./data/history.db`. One-minute averages for 2 days, ten-minute averages for 35 days, about 10 MB at most. The allowance and price apply to past history too, so calibrating later corrects old figures.
- **Gaps**: if readings stop for 10 seconds or more while the dashboard is running, the server was too bogged down to keep up. That stretch is shaded **red** on the History charts and the live graphs, with what the CPU, disks and memory were doing and which programs were busiest (hover the band to read it). You also get a "Server slowed down" alert (at most one every 15 minutes) and an entry in the activity log. Stretches when the dashboard or the server wasn't running (an update, a restart) are shaded **grey** and don't raise that alert. The 10 seconds can be changed in Settings → Alerts.

## Drive care

For the drives in each ZFS pool, LabKeel watches what ZFS itself says about each one, as well as its SMART health.

- **When a drive fails** (ZFS faults it, it disappears, or SMART says it's failing), you get an urgent alert naming it, a card at the top of Storage and Overview, and a reminder every 12 hours (Settings → Drive care) until it's replaced.
- **Replacing it**: choose **Replace…** on the card or the drive. The walkthrough shows the drive's name, serial number and slot so you pull the right one; takes it offline if it's still in use (skip this if there's a free slot: the pool then stays protected while it rebuilds); offers **Shut down to swap**; then waits for the new drive to appear. Pick it, confirm, and the rebuild starts, with progress and time left. You get a message when it's done.
- **Safety**: only drives that aren't mounted, aren't in another pool, aren't USB and are big enough are offered. A drive with anything on it needs its serial number typed before it's erased. The replacement is added by its permanent `/dev/disk/by-id` name, so the pool doesn't care if `nvme` numbers change.
- **Automatic rebuild** (off by default): if a blank drive at least as big as the failed one is plugged in while one is waiting, the rebuild starts by itself.
- **While a pool has no redundancy**, scheduled restarts are held, and any containers you choose (Immich, for example) are stopped and started again once the pool is healthy.
- **Scrubs**: start, pause or stop one from the pool. If none has finished in 35 days you get a reminder.
- **Self-tests**: run a short or long SMART self-test from any drive's details, or schedule them weekly (the first of the month can be a long one). A failed test raises an alert.
- **Clear errors**: after you've fixed a cause such as a loose cable, clear ZFS's error counts from the pool or the drive.
- Warning limits for temperature and SSD wear are in Settings → Drive care.

## Pools

Storage → a pool's **Manage** button (or its name) opens that pool's own page. It works the same for every pool, including ones you add later. **New pool…** and **Import a pool…** are at the top of the Pools section.

- **Overview**: space used and free (usable, after parity), how much compression saves, how much snapshots hold, read and write speed right now, last scrub and TRIM, and the layout: each group (RAIDZ, mirror, single drive) with its drives, their state, temperature, TRIM state and live speed. Select a drive for its health, or to replace it.
- **Datasets**: every dataset with its space, free space, snapshots, compression and where it's mounted, and which running containers use it. Select one to change its compression, record size, size limit (quota), access times, read-only, sync and snapshot-folder settings; to take a snapshot; to make a dataset inside it; or to delete it. "New dataset…" offers sensible record sizes for general files, big media files, databases and VM disks.
- **Snapshots**: every snapshot, newest first, with how much space each holds (what deleting it would free). Take one, delete several at once, or roll a dataset back. **Automatic snapshots** per dataset: choose how many hourly, daily, weekly and monthly ones to keep (the usual is 24, 14, 8 and 12). They're taken on the hour, at midnight, on Mondays and on the 1st, including any missed while the server was off, and the oldest beyond your numbers are deleted. Only snapshots the dashboard took itself (named `auto-…`) are ever deleted automatically.
- **Maintenance**: scrub and TRIM now, pause or stop them, and schedule either weekly or monthly. Automatic TRIM on or off. Pool options (autoexpand, autoreplace, what to do if drives stop answering, a note). Add a hot spare, a cache or log drive, or another group of drives matching the pool's layout; widen a RAIDZ group by one drive (OpenZFS 2.3 or later); mirror a single drive; remove spares, cache and log drives; detach a drive from a mirror. Turn on newer pool features. Export or destroy the pool.
- **History**: every change ZFS recorded for the pool, newest first (automatic snapshots hidden unless you ask).
- **New pool**: pick free drives (never mounted ones, ones in a pool, USB drives, or LVM/RAID/swap/encrypted ones), and the layout options show usable space and how many drives can fail for exactly those drives, with a suggestion. Choose a name, compression and sector size. Drives go in by their permanent `/dev/disk/by-id` names, with `ashift=12`, LZ4, `xattr=sa` and `relatime` by default, and automatic TRIM for SSDs.
- **Import**: finds pools on the drives that aren't imported: ones moved from another machine, exported, or destroyed by mistake (those can often be recovered). Datasets are mounted where they were, except anywhere that would cover a system folder (`/etc`, `/usr`, `/home`…) or a folder that already has files in it; those are left unmounted and listed.

**Safety.** Anything that erases data or can't be undone (destroying or exporting a pool, deleting datasets or snapshots, rolling back, keeping fewer automatic snapshots than there are, adding a group of drives, widening RAIDZ, turning on new features, or using a drive that isn't blank) needs the name typed **and your dashboard password again**. So do the changes that take away redundancy: detaching a drive from a mirror, removing a spare, cache or log drive, taking a drive offline, and replacing a drive that's still healthy. After you've given it, the same browser at the same address isn't asked again for 10 minutes. Wrong passwords count towards the same 5-tries pause as the sign-in page. So a stolen session cookie on its own can't delete your data or weaken the pool.

If a pool is imported with datasets left unmounted (because they'd have covered a system folder or a folder with files in it), those datasets are set to `canmount=noauto`, so they stay unmounted after the next restart too. Mount them yourself once you've chosen a safe place.

If one dataset's automatic snapshots fail (its pool is full, a drive is hanging), the others carry on, and the problem is logged and alerted once. ZFS commands that hang on a failing drive are given up on after their time limit instead of holding the dashboard up.

The dashboard refuses to export or destroy a pool, delete a dataset or roll one back while a running container uses it (it names them), or while Docker keeps its own files there. Pools and datasets can't be mounted over system folders. Names are checked against what ZFS reports at that moment, and drives are chosen by serial number from the free ones, never by a path from the browser.

Ubuntu's `zfsutils-linux` package already scrubs every pool on the second Sunday of the month (and TRIMs SSD pools on the first Sunday). The Maintenance tab says so when it finds that job; you can keep it, or schedule your own instead.

`LABKEEL_ALLOW_ZFS_CHANGES: "0"` makes all of this read-only, drive care's replace, offline and clear buttons included: the pages still show everything, but nothing in ZFS can be changed.

## Settings

Settings is split into pages: General, Security and sign-in, Alerts, Containers, Drives, Restarts and power, and Activity log.

- **Restart schedule:** daily, weekly, or on the first chosen weekday of each month, in the server's own time zone. It can hold the restart while someone is on Minecraft (any service with a `bedrock` check) or watching Plex (needs your Plex token). It keeps checking for the time you choose, then skips or restarts anyway. It can also restart only when Ubuntu says updates need it. It won't restart within 20 minutes of booting, and if the dashboard was off at the scheduled time, that restart is skipped rather than run late.
- **Dashboard:** how often the live figures update: every 0.3, 0.4, 0.5, 1, 2 (default) or 5 seconds. The server only reads them that fast while a dashboard page is open, so a fast setting costs nothing when nobody's looking.
- **Restart or shut down:** both ask for confirmation and your password first; restarting shows who's using the server. After a shut-down the server stays off until someone presses its power button.
- **Alerts:** to the ntfy app (`https://ntfy.sh/<a-long-random-topic>`, or your own ntfy server) or a Discord webhook. You're told when a drive is failing or hot, a pool degrades or finds damaged files, an automatic snapshot fails, a pool passes its fullness limit (80% by default: ZFS slows down when nearly full), a disk or a dataset with a size limit passes yours, a container crashes or keeps restarting, a service is down, the server slows down, or the server restarts. Each problem is sent once, plus a message when it clears. To avoid noise, a problem has to last before it's sent (a service 3 minutes, a container 2, a full disk 2) and has to stay fixed for 3 minutes before "back to normal". A full disk clears only once it's 2 points under your limit. If the same problem comes back within 30 minutes of clearing, it has to last 10 minutes before you're told again. **Quiet hours** hold anything that can wait and send it as one summary afterwards; a failing drive, a degraded pool or repeated wrong passwords still get through.
- **Drive care:** see above.
- **Weekly Docker clean-up:** removes images no container uses, plus build cache.
- **Activity log:** everything LabKeel does, and who did it, searchable.

Changes that would quietly lower the server's guard ask for your password again: changing where alerts go, turning any alert off, raising an alert limit, turning on or moving quiet hours, turning on automatic drive replacement, changing which containers are paused during a drive swap, or letting restarts go ahead during a rebuild. Everything else saves straight away.

Settings, names, history, certificates and the activity log live in `./data`. Back up that folder along with `config.json`. Files there are written safely (a power cut can't leave one half-written), and if one is ever damaged anyway, it's moved aside as `<name>.damaged-<time>` and defaults are used, rather than being overwritten.

Restarts need `pid: host` and `privileged: true`, which are already in the compose file. Set `LABKEEL_ALLOW_RESTART: "0"` to turn restarting off completely.

## Access and security

The dashboard can read the Docker socket and the folders you mount, and it can restart the server. Keep it off the open internet, use HTTPS, and set a password.

### HTTPS (on by default)

The dashboard serves HTTPS on its own port. Opening the old `http://` address redirects to the secure one. `LABKEEL_HTTPS` picks the certificate:

| `LABKEEL_HTTPS` | Certificate |
| --- | --- |
| `auto` (default) | Your own certificate if there's one in `./certs`. Otherwise a **trusted Tailscale certificate** for `<machine>.<tailnet>.ts.net`. Otherwise its own self-signed one: still encrypted, but browsers warn. |
| `tailscale` | Tailscale only. Errors out rather than falling back. |
| `off` | Plain HTTP. Only use this behind a proxy that does HTTPS, such as Caddy. |

If renewing the Tailscale certificate fails (Tailscale restarting, say), the dashboard keeps using the current one while it's still valid, retries every 10 minutes, and says so in Settings → Security. It never swaps a trusted certificate for its self-signed one.

**Turning on the trusted Tailscale certificate:** in the Tailscale admin console, open **DNS**, make sure MagicDNS is on, and under **HTTPS Certificates** choose **Enable HTTPS**. The dashboard picks it up within 10 minutes, or straight away after `docker compose restart`. Then use `https://<machine>.<tailnet>.ts.net:8787`; Settings → Security shows the exact address. It renews by itself, and the dashboard swaps in the renewed certificate without a restart. Other devices need Tailscale running to open that address.

**Your own certificate:** put `tls.crt` and `tls.key` (or `fullchain.pem` and `privkey.pem`) in `./certs`. After replacing them, run `docker kill -s HUP labkeel` to reload them.

**Behind Caddy instead:** set `LABKEEL_HTTPS: "off"` and `LABKEEL_LISTEN_ADDRESS: "127.0.0.1"`, and proxy to it:

```
labkeel.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

### Sign-in

The first time you open LabKeel it asks you to create an admin account, using a one-time code from its log
(`docker logs labkeel`). More accounts, admin or view-only, are added on the Account page. Passwords are
stored only as Argon2id hashes, never as readable text. A cookie keeps you signed in for 12 hours, or 30
days if you tick the box.

An admin can also be set in `docker-compose.yml` with `LABKEEL_ADMIN_USER` and `LABKEEL_ADMIN_PASSWORD_HASH`, a hash made with
`docker exec -it labkeel python accounts.py hash`. There is no way to give LabKeel a password as plain text.

Sign-in only happens over HTTPS. Over plain `http://`, LabKeel shows only a page explaining how to turn
HTTPS on (the Docker health check still works).

It opens without any sign-in only if you set `LABKEEL_ALLOW_NO_SIGN_IN: "1"` and have no accounts; listening on 127.0.0.1 alone is no longer enough, because other containers on the host network, a reverse proxy or a malicious web page (by DNS rebinding) can all reach 127.0.0.1. With sign-in off, every page shows a red "Sign-in is off" banner, and the dashboard only answers to this machine's own names and IP addresses; add any other name you use with `LABKEEL_HOSTNAMES: "dash.example.ts.net"` (comma-separated).

- After 5 wrong passwords from one address within 15 minutes, sign-in from that address pauses for 15 minutes, and you get an alert if alerts are set up. Each try is counted the moment it arrives, so sending many at once doesn't get extra guesses. If 100 wrong passwords arrive from all addresses together within 15 minutes, every sign-in pauses for a while (a `docker compose restart` clears it).
- Settings → Security has **Sign out** and **Sign out everywhere**. Signing out ends that session on the server too, so a copy of its cookie stops working at once. Sign out everywhere replaces the key that signs sessions, so every existing session stops working. Changing an account's password signs that account out everywhere else.
- **Behind Caddy or another proxy**, set `LABKEEL_TRUSTED_PROXY: "127.0.0.1"` (the proxy's address). LabKeel then believes the proxy about each visitor's real address (for the wrong-password pause) and about their connection being HTTPS (without which it won't sign anyone in). Forwarded headers from anywhere else are ignored, so nobody can fake them.

If `docker-compose.yml` holds `LABKEEL_ADMIN_PASSWORD_HASH`, keep it readable only by you: `chmod 600 docker-compose.yml`.

### Passkeys

Settings → Passkeys adds a passkey (Face ID, Touch ID, Windows Hello, an Android phone or a security key). Then the sign-in page has **Sign in with a passkey**, and the "enter your password" prompt for big changes has **Use a passkey**. The password keeps working as well. Passkeys only work over HTTPS at a name, not an IP address, and belong to the address they were made on: add them while using `https://<machine>.<tailnet>.ts.net:8787`. Only their public keys are stored (`./data/passkeys.json`). Adding or removing one asks for the password, and failed passkey attempts count towards the same 5-tries pause.

### Phone app

On an iPhone, open the dashboard in Safari, tap Share, then **Add to Home Screen**; on Android, Chrome's menu → **Add to Home screen** (or **Install app**). It opens full-screen with its own icon. Settings → Phone app sets the icon from any picture (cropped square, 512 pixels) or goes back to the built-in one. Re-add it to the home screen after changing the icon.

### Browser protections

- The session cookie is `__Host-` prefixed, Secure, HttpOnly and SameSite=Strict, so scripts can't read it and other sites can't send it or overwrite it.
- Changes (anything that isn't a plain read) are refused when they come from another website, including other sites on your tailnet, which browsers treat as "same-site".
- A strict Content Security Policy lets only the dashboard's own scripts run (matched by hash), so an injected script can't. Pages can't be framed. Requests over 1 MB are refused as the bytes arrive, whichever way they're sent, and a sign-in request over 16 KB is refused outright.
- Connections have time limits. A new one has 10 seconds to start talking, and one where nothing has moved either way for 2 minutes is closed. One address can hold at most 64 at a time (a browser uses about 6), and the whole dashboard at most 512 (`LABKEEL_MAX_CONNECTIONS`). So a slow or flooding client can't tie it up.
- Fonts are bundled, so pages load nothing from outside your server.
- Outgoing checks (services, Plex, alerts) only use `http://` or `https://` addresses. The Plex check doesn't follow redirects, so your Plex token is only ever sent to the address you entered.
- Pages, scripts and API answers are compressed, and scripts and styles are cached until they change.

### File browser

The file browser can change files in folders mounted read-write (`:rw`): new folders and files, uploads of any size (drag files onto the list; set `LABKEEL_UPLOAD_LIMIT_GB` only if you want a cap), uploading whole folders (Upload folder, or drop a folder onto the list; their structure is kept), searching a folder and everything inside it by name (type in the filter box and press Enter), copying (Copy…, Ctrl+C then Ctrl+V, or Ctrl-drag; copies run in the background and show in the strip at the top of the page, and a copy into the same folder is called "name (copy)"), renaming, moving anywhere (with the Move… button, or by dragging onto a folder in the list, the tree or the path at the top, which always asks first), and editing text files up to 512 KB. A move on the same drive or dataset is instant. A move to another drive is a background job shown in the strip at the top of the page: everything is copied (links as links, owners, permissions and times kept), the copy's size is checked, and only then is the original deleted. Stopping a move, running out of space or a restart part-way leaves the original untouched and removes the partial copy; one move runs at a time. Deleting moves things to a Bin (a hidden `.dashboard-trash` folder at the top of that drive) where they can be put back for 30 days (`LABKEEL_BIN_DAYS`), then they're deleted for good. The first change asks for your password, then not again for 10 minutes. New things belong to whoever owns the folder they're made in. Folders mounted `:ro` stay read-only, and `LABKEEL_ALLOW_FILE_CHANGES: "0"` turns changing files off everywhere. Every change is in the activity log (a burst of uploads, such as a whole folder, is one line). Replacing a file with an upload puts the old one in the Bin. Uploads and copies always leave 5% of the drive free (at most 10 GB), so the boot drive can't be filled by accident; uploads go one file at a time.

**Big uploads.** Files go up in 32 MB pieces. A piece that fails (Wi-Fi drop, the laptop sleeping, the dashboard restarting) is tried again, and what has already arrived is kept. Progress shows in a slim strip at the top of every page, so you can use the rest of the dashboard meanwhile; the browser asks before you close or refresh the tab while an upload is running. If the tab is closed anyway, the Files page shows **Unfinished upload** in that folder: upload the same file again and it carries on from where it stopped, or **Discard** it. Unfinished uploads untouched for 3 days are removed. The partly-uploaded file waits in the destination folder under a hidden name, so it never goes through the boot drive. With sign-in off, changing files is refused unless `LABKEEL_ALLOW_FILE_CHANGES: "1"` is set explicitly. Uploaded web pages and scripts are shown as plain text, never run, and every file opened from the browser is sandboxed.

Keyboard shortcuts work on the Files page (press `?` for the list): arrows, Enter, Backspace for up a folder, Space to select, Ctrl+A, Delete, F2 to rename, Ctrl+C / Ctrl+X / Ctrl+V, `/` to search, Shift+N for a new folder and U to upload.

The dashboard's own folder, hidden names (below), `.zfs` snapshot folders and the top-level folders themselves can't be changed, and each change is checked against the folder actually opened, so a link swapped in at the wrong moment can't redirect it. Folders and files matching `files.deny` in `config.json` (SSH keys, `.env` files, cloud credentials) are hidden and can't be opened, even by typing the path. Matching ignores upper and lower case, so `ID_RSA` counts as `id_rsa`.

The dashboard also hides its own folder: its `docker-compose.yml` can hold the admin's password hash, and `./data` holds the key that signs sessions. It finds that folder from its own container's mounts, so this works wherever you put it. To hide other folders as well, list them under `files.hide` in `config.json` as browser paths, for example `["/browse/Servers/homelab/secrets"]`.

ZFS snapshot folders (`.zfs`) can't be browsed: they hold old copies of everything, including the dashboard's own folder. Each file is checked again after it's opened, so swapping a folder for a link at just the wrong moment can't lead the browser somewhere hidden. If Docker can't be asked which folders are the dashboard's own, the file browser closes for a moment rather than guessing.

On the Storage page, ZFS datasets share the pool's space, so the pool's root and datasets using under 1 GB are left out of Filesystems. Use the eye button on any row to choose for yourself; your choices are saved.

## How it reads the host

| What | How |
| --- | --- |
| CPU, RAM, network | `psutil`, with `network_mode: host` and `pid: host` so the numbers are the server's |
| Containers | Docker socket, every 3 s while a page is open (every 10 s otherwise) |
| Filesystems, ZFS, drive-to-pool mapping | the host's own `df`, `zpool` and `lsblk`, run via `nsenter` |
| Drive health | `smartctl` inside the container, every 10 minutes (button to re-check). Uses `-n standby`, so a sleeping hard drive isn't spun up just to be checked. |

## Troubleshooting

- **"Can't reach Docker"**: the socket mount is missing or Docker isn't at `/var/run/docker.sock`.
- **No drives on Storage**: the container must be `privileged`.
- **No ZFS pools**: `nsenter` needs `pid: host` and `privileged`. Check `zpool list` works on the host.
- **Port 8787 taken**: change `LABKEEL_PORT`.
- **Browser says the connection isn't private**: you're on the self-signed certificate, or you used an address the certificate doesn't cover (such as the LAN IP). Turn on Tailscale HTTPS (above) and use the `ts.net` address.
- **Locked out after wrong passwords**: wait 15 minutes, or `docker compose restart`.
- Logs: `docker logs labkeel`.

## Working on the code

Every Python file starts with a description of what it does, and each function says what it's for.
`app/main.py` opens with a map of how the files fit together.

| Where | What's in it |
| --- | --- |
| `app/serve.py` | Starts LabKeel. With HTTPS on, one port takes both: encrypted connections go to the app, `http://` gets a redirect. |
| `app/main.py` | The web app: live CPU, memory and network, storage, drive health (SMART), the file browser's reading side, service checks, the Overview's "needs attention" list, the phone app's icon and manifest. |
| `app/env.py` | The settings from `docker-compose.yml` ([settings.md](settings.md)). |
| `app/security.py` | Sign-in, sessions, the wrong-password pause, the password-again check for big changes, blocking requests from other websites, security headers. |
| `app/accounts.py` | Accounts and their password hashes; `python accounts.py` manages them from the command line. |
| `app/passkeys.py` | Signing in, and confirming big changes, with a passkey. |
| `app/tls.py` | The HTTPS certificate: yours from `/certs`, Tailscale's, or a self-signed one; renewed in the background. |
| `app/db.py` | The SQLite database (activity log, update checks) and its migrations. |
| `app/upkeep.py` | Settings, scheduled restarts, alerts (and quiet hours), the Docker clean-up and the activity log. |
| `app/metrics.py` | GPU and power readings, the history behind the Overview charts, and spotting slowdowns. |
| `app/containers.py` | Docker containers: the list and their figures, logs, and the start, stop, pause and remove buttons. |
| `app/updates.py`, `app/ctrupdate.py` | Checking registries for newer images, and updating a container with a way back. |
| `app/selfupdate.py` | LabKeel updating itself from Settings. |
| `app/composefile.py` | Bringing your `docker-compose.yml` up to date with a new version: new settings, new names. |
| `app/apps.py` | The Apps page: the catalogue, writing an app's compose file, and adding and removing it. |
| `app/servers.py` | The Servers page: join codes, the agents' reports, and alerts about other machines. |
| `app/agent.py`, `agent.compose.yml` | The agent that runs on another machine and reports to this one. |
| `app/drivecare.py` | Following failing drives through to their replacement, scrubs, SMART self-tests. |
| `app/zfsmgr.py` | Pools and datasets: creating, importing, changing the layout, snapshots, scheduled scrubs and TRIMs. |
| `app/fileops.py` | Changing files: new folders and files, editing, resumable uploads, renaming, moving and copying, the Bin. |
| `app/version.py`, `VERSION` | LabKeel's version: change `VERSION` to release. |
| `app/healthcheck.py` | Docker's health check. |
| `app/static/` | The pages (`index.html` is the dashboard, plus sign-in, set-up and account pages), the default home-screen icon and the fonts. |
| `app/static/ui/` | The dashboard's scripts, one per page (`index.html` loads them in order), and its styles (`app.css`). |
| `tests/` | The tests: `python -m pytest`. They never touch the real machine. |

Everything LabKeel remembers is in `./data`: `settings.json`, `state.json` (schedules and open alerts),
`labkeel.db` (the activity log and update checks), `overrides.json` (names you've given things),
`drivecare.json`, `zfs.json`, `trash.json`, `moves.json`, `uploads.json`, `passkeys.json`, `revoked.json`,
`apps.json` (apps added from the Apps page), `servers.json` (your other machines, with their tokens hashed),
`session.key`, `history.db` (the charts), your home-screen icon and `tls/`.

Only Python packages that the code imports are in `requirements.txt`, each with a note saying what it's
for; the `Dockerfile` adds `smartctl` and `nsenter` from Debian's packages.

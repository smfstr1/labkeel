# Changelog

## 0.1.2-alpha

### Security
- **Accounts and roles.** Several accounts, each an **admin** or a **viewer** (status pages only).
  Managed on the new Account page, or with `python accounts.py` inside the container.
- **Passwords are stored only as Argon2id hashes**, in `data/users.json` (readable by its owner only).
  At least 12 characters; common passwords are refused.
- **First-run set-up page.** With no account, the dashboard asks you to create one, using a one-time
  code printed in its log.
- **Destructive features start off.** `LABKEEL_ALLOW_FILE_CHANGES`, `LABKEEL_ALLOW_ZFS_CHANGES` and `LABKEEL_ALLOW_RESTART`
  must now be set to `1` to turn them on.
- **Sign-in through a reverse proxy** (Authelia, Authentik, oauth2-proxy) with `LABKEEL_PROXY_USER_HEADER`, only
  believed from `LABKEEL_TRUSTED_PROXY`. Forwarded addresses are handled the same way.
- **HSTS**, opt-in with `LABKEEL_HSTS=1`.
- Signing in to one account no longer resets the wrong-password count for guesses at another.
- Sign-in takes the same time whether or not the user name exists.
- Passkeys belong to an account; confirming a big change only accepts your own.
- **`DASH_PASS` is removed.** The admin set in `docker-compose.yml` now needs `LABKEEL_ADMIN_PASSWORD_HASH`, an Argon2id hash,
  so a password is never stored there in plain text.
- **Sign-in only over HTTPS.** Over plain HTTP, LabKeel answers only its health check and a page explaining
  how to turn HTTPS on. Behind a reverse proxy, set `LABKEEL_TRUSTED_PROXY` so its HTTPS is recognised.

### Added
- **Your docker-compose.yml keeps up with updates.** `git pull` leaves it alone (it's your own copy of the
  example), so new settings never reached it. Now, after LabKeel updates itself, it adds any settings the new
  version lists, switched off, and gives old `DASH_` names their `LABKEEL_` ones. Your values, comments and
  layout stay as they are. Docker Compose checks the new file first, and the old one is kept as
  `docker-compose.yml.bak`. **Settings → LabKeel** shows when the file is behind and can do the same after an
  update by hand. See `docs/updates.md`.
- **Apps.** A catalogue of 18 popular self-hosted apps (Jellyfin, Plex, Immich, Home Assistant, the *arr apps,
  qBittorrent, Nextcloud, Vaultwarden, Pi-hole, Minecraft Bedrock and more), or your own compose file. Fill in a
  folder or two and LabKeel writes the compose file into the app's own folder (`/srv/labkeel/apps/<app>`),
  pulls and starts it, waits until it answers, and adds it to Services. Everything it makes is ordinary Docker
  Compose. Removing an app stops it and keeps its folder and data; adding it again picks them up. Off until
  `LABKEEL_ALLOW_APP_INSTALLS=1`; admins only, password again, every step logged. See `docs/apps.md`.
- **Several servers.** Add your other machines (a NAS, a Raspberry Pi, a second server): each runs LabKeel's
  small agent (`agent.compose.yml`), which joins with a one-time code and reports over HTTPS. The Servers page
  shows them all with totals, each has its own page (figures, containers, storage, drive health), a switcher
  sits beside the machine's name, **Containers → All servers** lists every container, and alerts cover them
  too. The agent checks it's talking to your LabKeel before sending anything, opens no port, and only reads.
  See `docs/servers.md`.
- **Update a container from its panel.** "Update now" pulls the newer image and re-creates the container with
  Docker Compose, checks the new one starts (and passes its health check), and puts the old image back if it
  doesn't. Admins only, password again, logged.
- **Choose the Overview's tiles.** Customise picks up to six of CPU, Memory, GPU, Containers, Network, Power and
  Storage, in any order, and what the second history chart shows.
- **Services: Logos, List or Detailed view.** Drop an image named after a service into `config/logos`
  (`jellyfin.svg`) to show its logo; otherwise it gets a coloured letter tile.
- **Settings in pages:** General, Security and sign-in, Alerts, Containers, Drives, Restarts and power,
  Activity log.
- A warning when LabKeel's data folder isn't mounted, which would lose history and settings on every rebuild.
- **LabKeel updates itself.** Settings → LabKeel shows the version, checks for a new one (the new commits on
  the branch you cloned, or a newer image) and installs it: `git pull` and `docker compose up -d --build`,
  run on the host by systemd so they survive LabKeel restarting. Admins only, asks for the password, and the
  activity log says who updated it and how it went. A failed update leaves the old version running.
- **Container update checks.** LabKeel asks each image's registry (Docker Hub, GHCR, lscr.io, Quay or any
  OCI registry) whether its tag now points to a newer image, every 6 hours by default (Settings → Container
  updates) or on demand with **Check for updates**. Containers that are behind get an **Update** badge and
  their own filter, and the container's panel says how to update it. Nothing is downloaded or changed. An
  alert for new versions can be turned on under Alerts. Only one small request per image, which doesn't
  count towards Docker Hub's pull limit.
- **More container controls.** Pause and resume, remove (asks for the password; volumes are kept), and the
  same for several containers at once. A **Needs attention** filter. Logs can be searched and limited to
  the last 15 minutes, hour or day.
- **The activity log names who did what**, and from which address, including for things that finish later
  (a queued file move, a batch of uploads, a restart). It's now kept in a database (`data/labkeel.db`) and
  can be searched; the old `events.json` is imported once.
- **Ready-made services.** `config.example.json` has templates for 22 popular apps (Plex, Jellyfin, Immich,
  the *arr apps, Home Assistant, Pi-hole, Minecraft and more), all off until you set `"enabled": true`,
  plus a blank one for your own. A new install no longer opens on a page of red "down" tiles.
- `"enabled": false` on any service hides it and stops its checks.
- `{host}` in a service's link becomes the address LabKeel was opened at, and in its check, the machine itself.
- `docs/services.md` explains every field, placeholder and check.

### Changed
- **On a phone, services open in their apps**: tapping Plex, Immich or Home Assistant on the Services page
  or the Overview opens the app instead of the web page, falling back to the web page when the app isn't
  installed. Any service can name its own app with `"app"` in config.json ([docs/services.md](docs/services.md)),
  and the **Open in apps** switch on the Services page turns it off on that phone. Names like "Plex Media
  Server" count too.
- **Settings in `docker-compose.yml` have clearer names, all starting `LABKEEL_`**: `LABKEEL_ALLOW_FILE_CHANGES`,
  `LABKEEL_ALLOW_ZFS_CHANGES`, `LABKEEL_ALLOW_RESTART`, `LABKEEL_HTTPS` and so on. The old `DASH_` names still
  work, and LabKeel lists any it finds in its log with their new names. [docs/settings.md](docs/settings.md)
  describes every setting. On/off settings now also take `true`/`false`, `yes`/`no` and `on`/`off`, and a value
  that can't be read is reported in the log instead of stopping LabKeel from starting.
- **Every page and update is faster**: each answer from LabKeel used to wait about 40 ms before being sent
  (Nagle's algorithm on its internal connection). It now takes a few milliseconds.
- The page fetches live figures every 10 seconds, and the container list every 20, while you're on a page that
  doesn't show them (instead of every 2 and 4). Arriving on the Overview or Containers brings them up to date
  straight away.
- Phones: tapping your initial in the top bar opens a menu with your account and **Sign out**. The tab bar has
  a solid background, and steps aside while you type.
- The guide (docs/guide.md) is up to date and no longer describes one particular server.
- Uploads, copies and moves show as one slim strip at the top of every page (and a percentage beside Files),
  instead of a box in the corner and a separate Moves panel. The Moves button is gone.
- Files on a phone: the search box on its own row, Folders, Upload and More (for everything else), and each
  file's size and date under its name.
- Storage: a livelier "all drives healthy" panel with a light per drive.
- The CPU thread bars on the Overview are centred; Settings sections all line up at the same width.
- The dashboard page is split up: `index.html` holds only the markup, the styles are in `static/ui/app.css`,
  and the script is one file per page in `static/ui/` (core, overview, containers, storage, zfs, services,
  apps, servers, files, dialogs, settings, main), formatted with Prettier. Browsers keep each file until it changes.
- Settings now holds off scheduled restarts for Minecraft players or Plex viewers only if you tick those
  boxes (they were on by default).
- The image is built on Python 3.13 and Debian 13 (trixie). Container logs are capped at 3 × 10 MB in the
  example compose file.
- CI tests on Python 3.12 and 3.13 against a real Docker, scans for risky code (bandit) and known
  vulnerabilities (pip-audit), and starts the built image to check it's healthy. Dependabot proposes
  dependency updates weekly.

### Fixed
- The Overview's charts and the Services and Storage lists below them now share the same two columns, so
  their middles line up. They also go to one column at the same width.
- Settings → Alerts is tidier: alerts are grouped (drives and storage, containers and services, servers),
  limits line up on the right, and quiet hours have their own card with a switch and full-size time boxes.
- Phones: on a short page (Files, some Settings pages) the top bar grew to fill half the screen, and Safari
  could leave the tab bar floating mid-screen. The top bar now stays put at its own height, sticky toolbars sit
  below it, and nothing hides under the notch, the home bar or the browser's toolbars.
- Phones: there was no way to sign out from the top bar.
- A side panel (the Bin, a drive's details) stayed open over the next page after going back or switching
  page; it now closes when you leave the page it belongs to.
- The status bar and browser toolbar on phones now match the page in the light theme too.
- Settings on a phone: the chosen page's tab scrolls into view; card buttons go under the heading instead of
  squeezing it.
- The activity log no longer repeats the name on "… signed in" lines.
- The plain-HTTP redirect now encodes the path it sends you back to, so an odd request can't add anything to
  the redirect or its page.
- After **Update now**, the container's Update badge and note now go straight away: its image is checked again
  before the update says it's done, and the open log panel follows the re-created container instead of freezing.
- With Docker's containerd image store, a container left on an old image after a pull showed as a local build;
  it now shows as having an update.
- Update checks could see only one name of an image that has several (Docker's containerd image store lists
  just one), so most images showed as "built locally" instead of being checked.
- On a machine with few CPU threads, the per-thread bars on the Overview stretched across the whole page.
- After starting, stopping or restarting a container, the list could take a few seconds to show it.
- Restoring a Bin item whose copy had gone crashed, and deleting it for good did nothing.
- The dashboard's config folder wasn't hidden from the file browser when mounted as a folder.

### Upgrading from the original dashboard
1. Add these to `environment:` to keep everything working as before:
   `LABKEEL_ALLOW_FILE_CHANGES=1`, `LABKEEL_ALLOW_ZFS_CHANGES=1`, `LABKEEL_ALLOW_RESTART=1`.
2. Replace `DASH_PASS` with `LABKEEL_ADMIN_PASSWORD_HASH` (from `docker exec -it <container> python accounts.py hash`),
   writing each `$` as `$$`, or remove both and create an account on the set-up page.
3. Everyone signs in once more (the session cookie's format changed). Existing passkeys keep working.
4. If you run with `LABKEEL_HTTPS=off` behind a proxy, set `LABKEEL_TRUSTED_PROXY` to the proxy's address, or
   nobody can sign in.

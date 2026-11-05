# Changelog

## Unreleased

### Security
- **Accounts and roles.** Several accounts, each an **admin** or a **viewer** (status pages only).
  Managed on the new Account page, or with `python accounts.py` inside the container.
- **Passwords are stored only as Argon2id hashes**, in `data/users.json` (readable by its owner only).
  At least 12 characters; common passwords are refused.
- **First-run set-up page.** With no account, the dashboard asks you to create one, using a one-time
  code printed in its log.
- **Destructive features start off.** `DASH_FILES_WRITE`, `DASH_ZFS_WRITE` and `DASH_ALLOW_REBOOT`
  must now be set to `1` to turn them on.
- **Sign-in through a reverse proxy** (Authelia, Authentik, oauth2-proxy) with `DASH_AUTH_HEADER`, only
  believed from `DASH_TRUSTED_PROXY`. Forwarded addresses are handled the same way.
- **HSTS**, opt-in with `DASH_HSTS=1`.
- Signing in to one account no longer resets the wrong-password count for guesses at another.
- Sign-in takes the same time whether or not the user name exists.
- Passkeys belong to an account; confirming a big change only accepts your own.
- **`DASH_PASS` is removed.** The admin set in `docker-compose.yml` now needs `DASH_PASS_HASH`, an Argon2id hash,
  so a password is never stored there in plain text.
- **Sign-in only over HTTPS.** Over plain HTTP, LabKeel answers only its health check and a page explaining
  how to turn HTTPS on. Behind a reverse proxy, set `DASH_TRUSTED_PROXY` so its HTTPS is recognised.

### Added
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
- The dashboard page is split up: `index.html` holds only the markup, the styles are in `static/ui/app.css`,
  and the script is one file per page in `static/ui/` (core, overview, containers, storage, zfs, services,
  files, dialogs, settings, main), formatted with Prettier. Browsers keep each file until it changes.
- Settings now holds off scheduled restarts for Minecraft players or Plex viewers only if you tick those
  boxes (they were on by default).
- The image is built on Python 3.13 and Debian 13 (trixie). Container logs are capped at 3 × 10 MB in the
  example compose file.
- CI tests on Python 3.12 and 3.13 against a real Docker, scans for risky code (bandit) and known
  vulnerabilities (pip-audit), and starts the built image to check it's healthy. Dependabot proposes
  dependency updates weekly.

### Fixed
- Update checks could see only one name of an image that has several (Docker's containerd image store lists
  just one), so most images showed as "built locally" instead of being checked.
- On a machine with few CPU threads, the per-thread bars on the Overview stretched across the whole page.
- After starting, stopping or restarting a container, the list could take a few seconds to show it.
- Restoring a Trash item whose copy had gone crashed, and deleting it for good did nothing.
- The dashboard's config folder wasn't hidden from the file browser when mounted as a folder.

### Upgrading from the original dashboard
1. Add these to `environment:` to keep everything working as before:
   `DASH_FILES_WRITE=1`, `DASH_ZFS_WRITE=1`, `DASH_ALLOW_REBOOT=1`.
2. Replace `DASH_PASS` with `DASH_PASS_HASH` (from `docker exec -it <container> python accounts.py hash`),
   writing each `$` as `$$`, or remove both and create an account on the set-up page.
3. Everyone signs in once more (the session cookie's format changed). Existing passkeys keep working.
4. If you run with `DASH_TLS=off` behind a proxy, set `DASH_TRUSTED_PROXY` to the proxy's address, or
   nobody can sign in.

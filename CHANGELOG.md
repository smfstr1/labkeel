# Changelog

## 0.4.5-alpha

### Added

- **Pages fill the screen.** Every page now uses the full width of the window, so a wide or ultrawide monitor
  isn't left half empty. Settings → Appearance → Page width can hold pages to a width in the middle instead
  (Wide, 2000 px, or Standard, 1480 px, as before).
- **Six more themes.** Ocean, Ember, Rose, Terminal (green on black), Mint and Lavender, making fifteen.
- **Your own touches.** Settings → Appearance also has an accent colour (seven, or the theme's own) for buttons,
  links and charts; the lettering (LabKeel's, the device's, or fixed-width); icons beside the names in the menu;
  and a switch for movement. Kept in the browser, per person, with one button to put them all back.

- **Stop or restart LabKeel from the Containers page.** LabKeel's own container now has red Restart and Stop
  buttons, on its app heading and in its drawer. Each opens a warning first that this is the dashboard itself.
  Stopping also has you type "stop" and give your password, because nothing on the page can start it again:
  that takes `sudo docker compose up -d` in LabKeel's folder. Afterwards a full-page message says what happened
  and the page returns by itself once LabKeel is back. LabKeel is still skipped when several containers are
  acted on at once, and still can't pause or remove itself.

- **The ZFS memory cache, shown for what it is.** ZFS keeps recently read files in memory and gives it back
  when something else needs it, but Linux counts it as used, so a server that streams films all day looked a
  quarter full. The cache is no longer counted as used memory: the Overview's memory card shows it separately,
  and Storage has a Memory cache section with how full it is. The memory graph steps down from this version on.
- **Set the cache's limit.** Storage → Memory cache → Change limit sets how big the cache may grow, or hands the
  choice back to ZFS. It applies straight away and is kept after a restart, in whichever file under
  `/etc/modprobe.d` already sets it. Admins only, with `LABKEEL_ALLOW_ZFS_CHANGES=1`; it asks for your password
  and is written to the activity log.
- **Remove a drive from the dashboard.** A drive that isn't mounted and isn't in a pool has a small cross on its
  tile on Storage, and a "Remove from the dashboard" button in its details. LabKeel then stops showing it,
  checking its health and alerting about it, and it stays removed after Re-check drives and after a refresh.
  Nothing on the drive is touched. Removed drives are listed under the drive tiles with "Show again", and one
  comes back by itself if it's later mounted or added to a pool. Admins only; it's written to the activity log.

- **Network traffic history.** Click the Network card on the Overview for download and upload over the last
  hour, day, week or month, how much was downloaded and uploaded, the busiest moments, and each network card now.
  LabKeel records network traffic from this version on.
- **Each network card in detail.** In that panel, every network card is a button showing whether it's connected
  and its speed now. Tap one for its addresses (IPv4, IPv6, router, hardware address), its connection (type,
  link speed, MTU, driver), packets, errors and dropped packets since the server started, and for Wi-Fi the
  network's name, band, signal strength and link rates. A Back button returns to the traffic history.
- **ownCloud opens in its phone app.** On a phone, ownCloud in Services, and an added app's Open button on the Apps
  page, open the ownCloud app when it's installed, as Plex and Immich already did.

### Fixed

- **The Power card's graph survives a refresh.** It was drawn only from what the page had seen since it opened, so
  it started empty after every reload. The server now keeps it, like the other cards.
- **Tidier wording.** "Other drives" on Storage is now "Non-RAID drives", and the explanations under the Containers
  table, under the memory cache and the "Measured:" line on the Power chart are gone.
- **A drive that can't be read is no longer called healthy.** A card reader or enclosure that answers with
  nothing, or doesn't answer at all, now shows as "Unknown" with a plain reason, instead of counting towards
  "All drives healthy" or showing the raw command that timed out. What's mounted on such a drive is still found.
- **App totals line up on the Containers page.** Each app's CPU and memory now sit under the CPU and Memory
  headings instead of beside its name, and LabKeel's red Restart and Stop buttons look the same size as the
  buttons on the other apps.
- **A pool's page works on a phone.** Dataset names, the apps using them and their limits were cut to a few
  letters. Each dataset now shows its full name and size, its labels in full beneath, and where it's mounted.
  Snapshots show their whole name, dataset and date; the five tabs fit the screen; and a dataset's settings sit
  one to a line so every choice can be read.
- **No more "TLS handshake error … EOF" in an app's log.** The Services check on an app served over HTTPS
  (ownCloud, Vaultwarden, Nextcloud) used to open a connection and hang up, which the app logged as a failed
  handshake every 30 seconds. It now finishes the greeting first. Your own port checks can do the same with
  `"tls": true` (see `docs/services.md`). Nothing else about the check changes: a port that answers is up.

## 0.2.2-alpha

### Added
- **Files on every machine.** With other machines joined (Servers), each one's shared folders are on the
  Files page as a group of their own, next to this machine's: browse, preview, download and search them, and
  where that machine allows it (`LABKEEL_ALLOW_FILE_CHANGES=1` in its `agent.local.yml`), upload, rename,
  delete and use its Bin, just as here. The machine is at the start of the path, and in the address
  (`#files:@<id>/media/films`), so links and Back work.
- **Two panes.** A switch at the top of Files puts a second folder beside the first, each on whichever machine
  you choose, with Swap. Copy or move between them with the buttons or by dragging across.
- **Copying and moving between machines**: Copy to another server…, the two panes, dragging onto another
  machine's folder, or copy and paste. It runs in the background with its progress at the top of every page,
  checks the target allows changes and has room, asks what to do about names already there (keep both,
  replace, skip), keeps each file's date, and **checks every file's SHA-256 once it lands**. A move deletes
  the originals (to their Bin) only after that. See `docs/servers.md`.
- **Search every machine** at once, with the results grouped by machine.
- A machine's page lists the folders it shares with Files, and whether LabKeel can change them.
- File changes made on another machine are in its activity log and in this one's, as "On nas: …".
- Uploads keep each file's own date (its "modified" time on your computer), instead of the time it arrived.
- Each Files pane shows its free space, and whether it can be changed.
- **Your docker-compose.yml keeps up with updates.** `git pull` leaves it alone (it's your own copy of the
  example), so new settings never reached it. Now, after LabKeel updates itself, it adds any settings the new
  version lists, switched off, and gives old `DASH_` names their `LABKEEL_` ones. Your values, comments and
  layout stay as they are. Docker Compose checks the new file first, and the old one is kept as
  `docker-compose.yml.bak`. **Settings → LabKeel** shows when the file is behind and can do the same after an
  update by hand. See `docs/updates.md`.
- **Apps.** A catalogue of 19 popular self-hosted apps (Jellyfin, Plex, Immich, Home Assistant, the *arr apps,
  qBittorrent, Nextcloud, Vaultwarden, Pi-hole, Minecraft Bedrock and more), or your own compose file. Fill in a
  folder or two and LabKeel writes the compose file into the app's own folder (`/srv/labkeel/apps/<app>`),
  pulls and starts it, waits until it answers, and adds it to Services. Everything it makes is ordinary Docker
  Compose. Removing an app stops it and keeps its folder and data; adding it again picks them up. Off until
  `LABKEEL_ALLOW_APP_INSTALLS=1`; admins only, password again, every step logged. See `docs/apps.md`.
- **HTTPS for the apps that need it.** Vaultwarden (your own Bitwarden-compatible password manager) and the new
  ownCloud (Infinite Scale) are only added over HTTPS, and their plain web port is never opened to the network.
  Choose Tailscale (its trusted certificate, served with `tailscale serve`, switched off again when you remove
  the app) or a certificate made here (a small Caddy container in front of the app). The dialog says if
  Tailscale can't be used and why. See `docs/apps.md`.
- **Containers page, tidied.** Containers are grouped by the app (compose project) they belong to. Each app's
  heading shows how many are running and its total CPU and memory, folds away with a click (remembered in the
  browser, with Collapse all / Expand all), has a tick box that selects all of its containers, and Start,
  Restart and Stop buttons for the whole app. The row of All / Running / Stopped / Needs attention / Updates
  buttons is now one **Filter** button that shows what's chosen and how many containers each choice has.
- **A clearer log.** A container's log is shown as a terminal: dark in every theme, with errors and warnings
  picked out, your search highlighted, shorter timestamps, a count of lines and errors, and labelled buttons for
  Live (follow or hold still), Wrap, Copy, Download and Refresh, plus a "Latest" button when you've scrolled up.
- **Movement.** Rows arrive in turn when a filter or search changes, apps glide open and shut, menus, the
  selection bar and messages ease in, and buttons respond to a press. All of it is switched off when the device
  asks for reduced motion.
- **ownCloud signs you in.** Added over Tailscale, ownCloud could sit on "Logging you in" for ever: signing in,
  it calls the address you open it by, and from inside Docker that address timed out. Inside its container that
  address now means the container itself, and the HTTPS in front hands it traffic over HTTPS. An ownCloud added
  before needs three lines changed by hand: see "ownCloud stuck on Logging you in" in `docs/apps.md`.
- **Nextcloud installs ready to use.** Adding Nextcloud now brings PostgreSQL, Redis and its background jobs
  with it, already connected, with a database password made for it and kept in a root-only `.env` (never in
  the compose file or the dialog). Its first page asks only for the admin's name and password. It's served over
  HTTPS like Vaultwarden (Tailscale, or a certificate made here) and told its own address, so there is no
  "untrusted domain" page. It uses the official `nextcloud:apache` image; a Nextcloud added before keeps
  working as it was. See `docs/apps.md`.
- **Choose where an app's data is kept.** The Add dialog has a "Where its data is kept" box: LabKeel's apps
  folder (as before), any mounted ZFS pool or dataset, or another drive, each with its free space. On a pool,
  LabKeel can make a dataset just for the app, with an optional size limit (a quota) and, if you tick it, that
  much space set aside (a reservation). The app's folders move there by themselves; its compose file stays with
  the others. Making a dataset also needs `LABKEEL_ALLOW_ZFS_CHANGES=1`; a folder on a pool or drive doesn't.
  Removing the app keeps the dataset and its data. See `docs/apps.md`.
- **Themes.** Settings → Appearance has nine themes: match the device, Dark, Light, Midnight (true black),
  Graphite, Forest, Dusk, Paper and High contrast. Every account chooses its own, viewers included (a viewer's
  Settings is this page only). Kept in the browser, so each device can differ.
- **Your own menu.** On the same page, put the pages in the menu in the order you use them and hide the ones you
  don't. A hidden page still opens from a link or its address; Settings can't be hidden.
- **Tidier file controls.** The Files toolbar is one row: filter and search on the left, Upload files and New
  folder on the right, and the less-used actions (New file, Upload folder, Folder size, Bin, Keyboard shortcuts)
  under More. The selection bar's Clear button sits apart from the actions, on the right.
- **Clusters.** A new page for machines you look after together. A **group of servers** is a name for machines
  LabKeel already knows, shown side by side with their load, containers and problems; nothing is installed or
  changed on them. **Docker Swarm** shows a Swarm's nodes, services and where each copy runs, whether its managers
  can still agree ("Can it survive a failure?"), and recent failed tasks. With `LABKEEL_ALLOW_CLUSTER_CHANGES=1`
  admins can start a Swarm, show the command to join a machine, drain a machine (after seeing what will move and
  what can't), add, scale, restart and remove services, and leave; every change is admin-only, asks for the
  password again and is logged, and a join token is never stored or logged. Kubernetes is shown as "later".
  See `docs/clusters.md`.
- **Several servers.** Add your other machines (a NAS, a Raspberry Pi, a second server): each runs LabKeel's
  small agent (`agent.compose.yml`), which joins with a one-time code and reports over HTTPS. The Servers page
  shows them all with totals, each has its own page (figures, containers, storage, drive health), a switcher
  sits beside the machine's name, **Containers → All servers** lists every container, and alerts cover them
  too. The agent checks it's talking to your LabKeel before sending anything and opens no port; the
  machine's containers and settings are shown, not changed. See `docs/servers.md`.
- **Fold a pool away.** The arrow beside a pool's name on the Storage page folds it down to one line
  (name, health and how full it is). This browser remembers which pools you've folded.

### Faster and lighter
- **LabKeel uses about half the CPU it did** (idle, 0.5% of a core instead of 1.1%; with the Overview open,
  1.2% instead of 2.3%) and about 20 MB less memory. Most of it went on re-reading the machine's whole
  certificate store for every service check; now one TLS setting is made once and shared.
- **The agent uses a seventh of the CPU** (0.2% of a core instead of 1.4%) and about 25 MB less memory: it makes
  its TLS settings once, checks a hub with a self-signed certificate with one handshake instead of two, and keeps
  its connections to the hub open between requests, so a file request's answer goes back on the connection it
  came in on. Opening a folder on another machine takes about 11 ms instead of about 60.
- **The page loads a quarter of the bytes the first time** (and after each update): its scripts and styles are
  sent gzipped, 138 KB instead of 487 KB, compressed once and kept rather than on every visit.
- Docker's health check doesn't read the certificate store every 30 seconds; calls to Docker no longer look for
  proxy settings and `~/.netrc` each time; CI skips the slow arm64 build on pull requests.

### Security
- **Files on other machines** are reached only through a fixed list of file requests, checked on both ends,
  and run by the agent with LabKeel's own file code and path checks. The password is asked on this machine and
  never sent on. What comes back is sandboxed, and anything but a file is served as JSON, so another machine
  can't serve a page that runs here. Admins only, as Files here.
- An agent's shared folders go in `agent.local.yml`, which git leaves alone (`agent.local.example.yml` shows how).
- **An agent is trusted less than this machine.** Only a short list of its reply headers are passed on, a reply
  can't be longer than it says it is, the list of files it sends for a copy is checked before anything is
  written (so it can't steer a copy outside the folder you picked), what it relays to the activity log is
  cleaned and limited to 300 lines an hour, and it can't hold open more than 16 waiting requests.
- **Every field of an agent's report has a set type and length** (the machine's name and OS are text, its
  CPU and disks numbers, and so on). Anything else is dropped, so a broken or hostile agent can't put markup on
  the hub's pages or stop the Servers page and alerts working.
- **TLS 1.2 or 1.3 only, with forward secrecy and AES-GCM or ChaCha20**, set by LabKeel itself rather than left
  to the machine's OpenSSL. The older CBC ciphers that some systems still allowed are refused.
- **No request can crash a route.** The tests now send every route badly shaped input (the wrong types, broken
  JSON, JSON nested thousands deep), and each answers with a clear error. Before, six failed with a 500 on a
  field of the wrong type, two on a machine without ZFS, most on JSON nested a few thousand deep (the error
  repeated it back), and an agent joining on JSON nested deeper still. The error for a request of the wrong shape no longer
  repeats what was sent.
- An agent's connection to the hub never reconnects by itself: every new connection is checked first.
- **Nothing private is committed.** A test checks every file git would publish for private keys, tokens, password
  hashes, email addresses, home folders, real Tailscale names, and hidden details in images (camera, place,
  author), and CI runs gitleaks over every commit in the history. The tests and examples use neutral names.

### Changed
- Pydantic 2.14.3 (faster checking of requests).
- The example `docker-compose.yml`, `agent.compose.yml` and `agent.local.example.yml` are tidied, and every
  command for updating an agent keeps your `agent.local.yml` (without it, the agent would start again without
  its shared folders).

### Fixed
- A side panel (the Bin, a drive's details) stayed open over the next page after going back or switching
  page; it now closes when you leave the page it belongs to.
- The Overview's strip of per-thread CPU boxes is labelled "CPU, each thread", sits in a row beside its label
  instead of a stack, and is hidden on a machine with a single thread.
- The activity log and Files messages say "Bin" throughout (some said "trash"), and small sizes are given in KB
  or MB instead of "0.00 GB".
- Going from a folder on another machine to one on this machine (a link, Back, or this machine's name in the
  tree) stayed on the other machine.
- Machines without ZFS: the list of pools you could import failed instead of saying ZFS isn't installed.
- Testing alerts with no alert address set now says to add one, rather than reporting a failed delivery.
- Run Docker clean-up, when Docker can't be reached, says so (503) instead of reporting a fault in LabKeel.
- **A machine whose files can't be reached now says why**, instead of "(unavailable)": offline, its agent needs
  updating, not connected, or it shares no folders (the full reason shows on hover). Its agent's log says which
  folders it shares and whether changes are allowed, and why file requests can't get through (a hub that's
  older than 0.2.2 is named as the reason). The server page explains an agent too old to share folders, and every
  hint points at `agent.local.yml`.
- The message about passkeys needing a name gave a particular machine's name as the example.

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

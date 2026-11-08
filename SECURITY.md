# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's **Report a vulnerability**
button on the repository's Security tab, which sends a private report. Include what you found, how to
reproduce it, and which version you ran. You'll get a reply within a week.

## What LabKeel can do, and why that matters

LabKeel runs with privileged access to the machine. Depending on what you turn on, an admin can
restart or shut down the machine, create and destroy ZFS pools, change any file in the folders you
mount, and add apps whose containers can have full control of the machine. Treat an admin account like a root
password.

## How it protects that

| Area | What it does |
| --- | --- |
| Locked by default | Every request passes one gatekeeper; only the sign-in, set-up, health check and static files are public, plus the few addresses agents on your other machines use (to join, to report, and for file requests), which need a join code or that agent's own token. New routes are protected automatically, and the tests sweep every route to check. |
| First run | Nothing works until an account exists. The set-up page needs a one-time code printed in the container's log, so whoever reaches the page first can't claim it. |
| Passwords | Stored only as salted Argon2id hashes in `data/users.json` (readable by its owner only), or given as a hash in `LABKEEL_ADMIN_PASSWORD_HASH`. Never accepted or stored as readable text. At least 12 characters; common passwords refused. |
| Roles | **Admin**: everything. **Viewer**: the status pages only; no changes, files, container logs, process list or settings. Enforced on the server for every request. |
| Sign-in limits | 5 wrong passwords from one address pause it for 15 minutes (with an alert); 100 from everywhere together pause all sign-in. Signing in to one account doesn't reset guesses at another. Sign-ins take the same time whether or not the name exists. |
| Sessions | Signed cookies tied to the account: `__Host-` prefixed, Secure, HttpOnly, SameSite=Strict. A new password or role ends that account's other sessions; signing out revokes the cookie even if it was copied. |
| Big changes | Destroying data, changing accounts and similar ask for your password (or passkey) again, valid for 10 minutes. |
| Passkeys | WebAuthn with user verification, each tied to one account. |
| Other websites | Changes are refused when the browser says they came from another site (`Origin`, `Sec-Fetch-Site`). |
| Pages | A strict Content Security Policy: only each page's own script may run. No framing. Uploaded web pages are shown as text, never run. |
| Files | Can't leave the mounted folders, follow links out of them, or open SSH keys, `.env` files, ZFS snapshot folders or LabKeel's own data. Changing files is off unless `LABKEEL_ALLOW_FILE_CHANGES=1`. |
| Off by default | File changes, ZFS changes, restarting the machine and adding apps each need turning on. |
| Apps | Adding or removing an app is admin-only, asks for the password again, and is logged step by step. LabKeel writes only into the app's own folder (`LABKEEL_APPS_DIR`), runs fixed `docker compose` commands there, refuses system folders, whole top-level folders and ports already in use, and never takes over a folder it didn't make. Values you type go into the compose file quoted, with `$` escaped, so they can't add settings of their own. An app asking for full control of the machine (Home Assistant) says so before you add it. See [docs/apps.md](docs/apps.md). |
| Other machines | An agent joins with a code that works once, for 15 minutes, made by an admin with their password. Each agent then has its own token, kept on the hub only as a hash, and removing the machine revokes it. The agent checks the hub's certificate key before sending anything, and opens no port. Wrong codes are limited like wrong passwords, counted separately. Every field of an agent's report has a set type and length, and anything else is dropped. The agent's connections never reconnect by themselves: each new one is checked first. See [docs/servers.md](docs/servers.md). |
| Files on other machines | The hub can ask an agent only for the file requests on a fixed list (`filesapp.FILE_ROUTES`), checked on both ends, which the agent runs with LabKeel's own file code and its path checks, in the folders mounted under `/browse` there. Changes need `LABKEEL_ALLOW_FILE_CHANGES=1` on that machine. Admins only; the password is asked on the hub and never forwarded (the agent is told who's asking, and a hash of their sign-in). Nothing else the hub could send reaches the agent's containers, settings or machine. An agent is trusted less than the hub. Coming back, only a short list of headers passes, an answer can't be longer than it says, every answer is sandboxed (`Content-Security-Policy: sandbox`) and anything but a file is served as JSON, so a compromised agent can't serve a page that runs on the hub. The list of files an agent sends for a copy is checked before anything is written, so it can't steer a copy outside the folder you chose; what it relays to the activity log is cleaned and limited to 300 lines an hour; and it can hold at most 16 requests open. Copies between machines check every file's SHA-256 where it lands, and a move deletes originals (to their Bin) only after that. See [docs/servers.md](docs/servers.md#files-on-your-other-machines). |
| HTTPS only | Sign-in only happens over HTTPS: TLS 1.3, or TLS 1.2 with forward secrecy and AES-GCM or ChaCha20 ciphers, set by LabKeel itself rather than left to the machine's OpenSSL. Plain `http://` is redirected to HTTPS, or, with HTTPS turned off, refused: only the health check answers. Behind a proxy, its word that the visitor used HTTPS is only taken from `LABKEEL_TRUSTED_PROXY`. HSTS is opt-in (`LABKEEL_HSTS=1`) because it applies to every port on the host name. |
| Behind a proxy | Forwarded addresses, and sign-in by header, are only believed from `LABKEEL_TRUSTED_PROXY`. |
| Updating itself | Admins only, password again. Runs fixed commands (`git pull --ff-only`, `docker compose`) for LabKeel's own compose project, read from Docker's labels on its container; every value is shell-quoted, and the script is kept in `data/update.sh` to read. Git never overwrites local edits. Afterwards it brings `docker-compose.yml` up to date: it only adds settings at their defaults (off) and renames old ones, checks with `docker compose config` that LabKeel's environment is otherwise exactly as it was, keeps the file's owner and permissions, and saves the old one as `docker-compose.yml.bak`. |
| Bad requests | Every route answers malformed input (the wrong types, broken JSON, JSON nested thousands deep) with a clear error, without repeating what was sent; the tests send every route such input. |
| Activity log | Records who did what, from which address, for every change, including ones that finish later in the background. |

## What it connects to

LabKeel makes no outgoing connections of its own except these:

- **Image registries** (Docker Hub, GHCR and the like), to check for container updates: anonymous, HTTPS only,
  never with a password. Turn it off in Settings → Container updates. See [docs/updates.md](docs/updates.md).
- **Where LabKeel came from**, to check for a new version of itself: `git fetch` from the repository you cloned
  (as the folder's owner, with their own git sign-in), or the registry of the image you run.
- **Your alert address** (ntfy or Discord), when you've set one.
- **Your services**, for their up/down checks, as listed in `config.json`.
- **Plex**, at the address in Settings, only when scheduled restarts should wait for people watching.
- **Tailscale**, on this machine, for an HTTPS certificate when Tailscale is installed.
- **When you add an app**: its images, from their registries (`docker compose pull`), and for Immich, its
  compose file from Immich's latest GitHub release.

An agent on another machine connects only to your LabKeel, at the address you gave it.

## Your part

- Keep LabKeel off the open internet. Use a VPN such as Tailscale or WireGuard, or a reverse proxy with its own sign-in.
- Give out admin accounts sparingly; use viewer accounts for everyone else.
- If you set an admin in `docker-compose.yml`, it's with `LABKEEL_ADMIN_PASSWORD_HASH`: a hash, never the password itself.
- Forgot the password? On the server: `docker exec -it labkeel python accounts.py passwd <name>`.

## Checking it yourself

```sh
pip install -r requirements-dev.txt
python -m pytest tests/test_security.py
pip install bandit pip-audit
bandit -q -r app -ll            # risky code; reviewed exceptions are marked "nosec" with a reason
pip-audit -r requirements.txt   # known vulnerabilities in the dependencies
```

CI runs all of these on every change, and gitleaks over every commit in the history. `tests/test_release.py` checks
that nothing private is committed: no keys, tokens, password hashes, email addresses, home folders or real
Tailscale names, and no hidden details in the images.

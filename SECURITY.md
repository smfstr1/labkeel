# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's **Report a vulnerability**
button on the repository's Security tab, which sends a private report. Include what you found, how to
reproduce it, and which version you ran. You'll get a reply within a week.

## What LabKeel can do, and why that matters

LabKeel runs with privileged access to the machine. Depending on what you turn on, an admin can
restart or shut down the machine, create and destroy ZFS pools, and change any file in the folders you
mount. Treat an admin account like a root password.

## How it protects that

| Area | What it does |
| --- | --- |
| Locked by default | Every request passes one gatekeeper; only the sign-in, set-up, health check and static files are public. New routes are protected automatically, and the tests sweep every route to check. |
| First run | Nothing works until an account exists. The set-up page needs a one-time code printed in the container's log, so whoever reaches the page first can't claim it. |
| Passwords | Stored only as salted Argon2id hashes in `data/users.json` (readable by its owner only). At least 12 characters; common passwords refused. |
| Roles | **Admin**: everything. **Viewer**: the status pages only; no changes, files, container logs, process list or settings. Enforced on the server for every request. |
| Sign-in limits | 5 wrong passwords from one address pause it for 15 minutes (with an alert); 100 from everywhere together pause all sign-in. Signing in to one account doesn't reset guesses at another. Sign-ins take the same time whether or not the name exists. |
| Sessions | Signed cookies tied to the account: `__Host-` prefixed, Secure, HttpOnly, SameSite=Strict. A new password or role ends that account's other sessions; signing out revokes the cookie even if it was copied. |
| Big changes | Destroying data, changing accounts and similar ask for your password (or passkey) again, valid for 10 minutes. |
| Passkeys | WebAuthn with user verification, each tied to one account. |
| Other websites | Changes are refused when the browser says they came from another site (`Origin`, `Sec-Fetch-Site`). |
| Pages | A strict Content Security Policy: only each page's own script may run. No framing. Uploaded web pages are shown as text, never run. |
| File browser | Can't leave the mounted folders, follow links out of them, or open SSH keys, `.env` files, ZFS snapshot folders or LabKeel's own data. Changing files is off unless `DASH_FILES_WRITE=1`. |
| Off by default | File changes, ZFS changes, and restarting the machine each need turning on. |
| HTTPS | On by default (TLS 1.2 and 1.3 only). Plain `http://` is redirected. HSTS is opt-in (`DASH_HSTS=1`) because it applies to every port on the host name. |
| Behind a proxy | Forwarded addresses, and sign-in by header, are only believed from `DASH_TRUSTED_PROXY`. |

## Your part

- Keep LabKeel off the open internet. Use a VPN such as Tailscale or WireGuard, or a reverse proxy with its own sign-in.
- Give out admin accounts sparingly; use viewer accounts for everyone else.
- If you set an admin in `docker-compose.yml`, use `DASH_PASS_HASH`, not `DASH_PASS`, so the password isn't stored there in plain text.
- Forgot the password? On the server: `docker exec -it labkeel python accounts.py passwd <name>`.

## Checking it yourself

```sh
pip install -r requirements-dev.txt
python -m pytest tests/test_security.py
```

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
- `DASH_PASS_HASH` holds an Argon2id hash, so the admin set in `docker-compose.yml` needn't be in plain text.

### Fixed
- Restoring a Trash item whose copy had gone crashed, and deleting it for good did nothing.
- The dashboard's config folder wasn't hidden from the file browser when mounted as a folder.

### Upgrading from the original dashboard
1. Add these to `environment:` to keep everything working as before:
   `DASH_FILES_WRITE=1`, `DASH_ZFS_WRITE=1`, `DASH_ALLOW_REBOOT=1`.
2. `DASH_USER` and `DASH_PASS` still work, as an admin account. Better: replace `DASH_PASS` with
   `DASH_PASS_HASH` (from `docker exec -it <container> python accounts.py hash`), writing each `$` as `$$`.
3. Everyone signs in once more (the session cookie's format changed). Existing passkeys keep working.

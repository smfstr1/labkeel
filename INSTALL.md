# Installing LabKeel

Welcome aboard. In about ten minutes you'll have one page that shows your whole server: how hard it's
working, what's running, how your drives are feeling, and whether everything you host is still up.

No YAML wizardry needed. Let's set sail.

---

## 1. Check you've got the essentials

You need:

- **A Linux machine** to watch. A home server, a mini PC or a Raspberry Pi are all fine.
- **Docker Engine with the Compose plugin.** Check with:
  ```sh
  docker compose version
  ```
  If that prints a version, you're good. If not, follow [Docker's install guide](https://docs.docker.com/engine/install/).
- **git**, to fetch the code (`git --version` to check).

> **Heads up:** use Docker *Engine*, not Docker *Desktop*. Desktop runs containers inside a hidden
> virtual machine, so LabKeel would end up proudly reporting that VM's stats instead of your server's.
> Rootless Docker and Podman can't give it the access it needs either.

---

## 2. Grab the code

```sh
git clone https://github.com/smfstr1/labkeel.git
cd labkeel
```

Then make your own copies of the two example files:

```sh
cp docker-compose.example.yml docker-compose.yml
mkdir -p config && cp config.example.json config/config.json
```

Your copies are yours to edit. Git ignores them, so updates will never overwrite your settings.

---

## 3. Make it yours

Open `docker-compose.yml` in your favourite editor (`nano docker-compose.yml` works fine).

**Pick the folders to browse.** At the bottom, each line under `volumes:` that ends in `/browse/...` shows
up in the Files page:

```yaml
      - /srv/media:/browse/media              # your folder : what LabKeel calls it
      - /home/you/documents:/browse/docs:ro   # :ro = look, don't touch
```

**Choose your powers.** LabKeel starts out careful. These three are off until you switch them on:

| Setting | Set to `1` to let LabKeel… |
| --- | --- |
| `DASH_FILES_WRITE` | upload, edit, move and delete files |
| `DASH_ZFS_WRITE` | create and change ZFS pools, datasets and snapshots |
| `DASH_ALLOW_REBOOT` | restart and shut down the machine |

Not sure? Leave them at `0`. You can watch everything without them, and switch them on later.

**Optional:** list the things you run in `config/config.json`, so the Services page can link to them and
check they're up. The example file shows a few kinds to copy from.

---

## 4. Launch

```sh
docker compose up -d --build
```

The first build takes a few minutes while Docker fetches Python and friends. Make a cup of tea.
Later starts take seconds.

---

## 5. Find your set-up code

LabKeel won't let just anyone become captain. The first account needs a one-time code that only
someone with access to the server can see:

```sh
docker logs labkeel
```

Look for this:

```
========================================================================
  No account yet. Open the dashboard in your browser to set one up.
  One-time set-up code:  7KQM-R2XH-9TNP
========================================================================
```

A new code is made each time LabKeel starts, so if you see several, use the newest.

---

## 6. Open it and create your account

In your browser, go to `https://<your server's address>:8787`.

- **With Tailscale:** use `https://<device-name>.<tailnet>.ts.net:8787`. If HTTPS certificates are turned
  on for your tailnet (Tailscale admin console → **DNS** → **HTTPS Certificates**), LabKeel fetches a
  proper certificate by itself and your browser won't complain.
- **Without Tailscale:** use the server's IP address or name. Your browser will warn that the connection
  "isn't private". That's LabKeel's own certificate, not a pirate. Choose **Advanced → Continue** once on
  each device.

Enter the set-up code, pick a user name and a password of **at least 12 characters**, and you're in.

> **Password idea:** a few unrelated words, like `plum orbit kettle seventy`, are long, strong and far
> easier to remember than `P@ssw0rd!`.

---

## 7. Invite the crew (optional)

Click your name at the bottom of the sidebar to open **Account**. From there you can add people as:

- **Admin**: can do everything. Treat it like the keys to the server.
- **Viewer**: sees the Overview, Containers, Storage and Services, but can't change anything, open
  files or peek at settings. Perfect for the family member who just wants to know if Plex is down.

Admins can also add a **passkey** in **Settings → Passkeys**, to sign in with Face ID, Touch ID, Windows
Hello or a phone instead of typing a password.

---

## Keeping it up to date

From the `labkeel` folder:

```sh
git pull
docker compose up -d --build
```

Your accounts, settings and history stay put.

---

## When things go sideways

| What's happening | What to do |
| --- | --- |
| It keeps restarting, and the log says "address already in use" | Something else is using port 8787. Add `- DASH_PORT=8788` under `environment:`. |
| Browser says the connection isn't private | Expected without a trusted certificate. Continue once, or turn on Tailscale HTTPS (step 6). |
| Lost the set-up code | Run `docker logs labkeel` again, or `docker compose restart` for a fresh one. |
| Forgot your password | `docker exec -it labkeel python accounts.py passwd <your name>` |
| "Too many wrong passwords" | Wait 15 minutes, or `docker compose restart`. |
| Drive health is empty | Normal inside a virtual machine: VMs don't share their drives' health with guests. |
| Something else | `docker logs labkeel` usually says what's wrong. If not, open an issue on GitHub. |

---

## Abandon ship (uninstalling)

No hard feelings. From the `labkeel` folder:

```sh
docker compose down --rmi local     # stops LabKeel and removes the container and its image
cd .. && rm -rf labkeel             # removes the code, accounts, settings, history and certificates
```

That's the lot. LabKeel keeps everything inside that folder.

Two things can be left elsewhere, but only if you switched them on:

- **File changes on:** deleted files go to a hidden `.dashboard-trash` folder inside each shared folder.
  Check for those if you want them gone too.
- **ZFS changes and automatic snapshots on:** its `auto-…` snapshots stay on your pool until you
  delete them with `zfs destroy`.

---

Smooth sailing. If LabKeel saved you a trip to the server cupboard, a star on GitHub is always appreciated.

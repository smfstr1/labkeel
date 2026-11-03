# Installing LabKeel

Welcome aboard. In about ten minutes you'll have one page that shows your whole server: how hard it's
working, what's running, how your drives are feeling, and whether everything you host is still up.

No YAML wizardry needed. Let's set sail.

---

## 1. Get the essentials

LabKeel needs three things on the machine it watches:

| What | Why | Check you have it |
| --- | --- | --- |
| **Docker Engine** | runs LabKeel | `docker --version` |
| **Docker Compose plugin** | starts it with one command | `docker compose version` |
| **git** | fetches the code, and updates later | `git --version` |

If all three print a version, skip ahead to [step 2](#2-grab-the-code). Everything else LabKeel uses
(Python, drive-health tools and so on) comes packed inside its own image.

> **Heads up:** use Docker *Engine*, not Docker *Desktop*. Desktop runs containers inside a hidden
> virtual machine, so LabKeel would end up proudly reporting that VM's stats instead of your server's.
> Rootless Docker and Podman can't give it the access it needs either.

Missing something? Open the box for your system. Each one installs the **latest** Docker straight from
Docker's own repository (your distribution's built-in `docker.io` package is often well behind), plus git.

<details>
<summary><b>Ubuntu</b> 22.04, 24.04, 26.04 or 26.10 (also Linux Mint and Pop!_OS)</summary>

First clear out any old or unofficial Docker packages, so they don't clash:

```sh
sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc | cut -f1)
```

Add Docker's repository:

```sh
sudo apt update
sudo apt install ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
```

Install Docker and Compose:

```sh
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Docker starts by itself, and on every boot from now on.

</details>

<details>
<summary><b>Debian</b> 12 or 13 (also Raspberry Pi OS 64-bit)</summary>

> **Raspberry Pi owners:** you need the **64-bit** Raspberry Pi OS. Docker stopped making new versions
> for the 32-bit one, so if `uname -m` says `armv7l`, it's time for a fresh 64-bit install.

First clear out any old or unofficial Docker packages:

```sh
sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-doc podman-docker containerd runc | cut -f1)
```

Add Docker's repository:

```sh
sudo apt update
sudo apt install ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
```

Install Docker and Compose:

```sh
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Docker starts by itself, and on every boot from now on.

</details>

<details>
<summary><b>Fedora</b> 43, 44 or 45</summary>

Clear out any old Docker packages:

```sh
sudo dnf remove docker docker-client docker-client-latest docker-common docker-latest \
  docker-latest-logrotate docker-logrotate docker-selinux docker-engine-selinux docker-engine
```

Add Docker's repository, install everything, and start Docker (now and on every boot):

```sh
sudo dnf install git
sudo dnf config-manager addrepo --from-repofile https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

If it asks you to accept a GPG key, check it matches the fingerprint on
[Docker's Fedora page](https://docs.docker.com/engine/install/fedora/) before saying yes.

</details>

<details>
<summary>Something else?</summary>

Docker's [install page](https://docs.docker.com/engine/install/) covers other distributions. Install
**Docker Engine** with the **docker-compose-plugin**, plus git from your package manager, then come back here.

</details>

### Make sure Docker works

```sh
sudo docker run hello-world
```

A cheerful "Hello from Docker!" means you're ready.

### Optional: use Docker without typing `sudo`

The rest of this guide leaves out `sudo` before `docker`. To do the same, add yourself to the `docker` group:

```sh
sudo groupadd docker          # fine if it says the group already exists
sudo usermod -aG docker $USER
newgrp docker                 # or sign out and back in
```

Membership of the `docker` group is as powerful as root, so only add people you'd trust with the whole
machine. Prefer not to? Just keep putting `sudo` in front of each `docker` command.

### Optional: Tailscale, for a trusted HTTPS address

With Tailscale, LabKeel gets a real certificate for a name like `server.your-tailnet.ts.net`, so browsers
won't warn and passkeys just work. Install it and join your tailnet:

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up             # prints a link: open it to sign in
```

Then turn on HTTPS for your tailnet, once: in the [Tailscale admin console](https://console.tailscale.com/admin/dns),
open **DNS**, make sure **MagicDNS** is on, and under **HTTPS Certificates** choose **Enable HTTPS**.

> HTTPS certificates are listed in public logs, so Tailscale publishes your machine names and tailnet name
> there. If a machine name gives away something private, rename it first.

Install Tailscale directly on the machine, not in a container: LabKeel asks the machine's own Tailscale
for its certificate.

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

- **With Tailscale:** use `https://<device-name>.<tailnet>.ts.net:8787`. With HTTPS turned on for your
  tailnet (see [step 1](#optional-tailscale-for-a-trusted-https-address)), LabKeel fetches a proper
  certificate by itself and your browser won't complain.
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

Docker and git update with the rest of your system (`sudo apt upgrade`, or `sudo dnf upgrade` on Fedora).
Since Docker now comes from Docker's own repository, that keeps you on the latest version too.

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

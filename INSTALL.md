# Installing LabKeel

Welcome aboard. In about ten minutes you'll have one page that shows your whole server: how hard it's
working, what's running, how your drives are feeling, and whether everything you host is still up.

No YAML wizardry needed. Let's set sail.

---

## 1. Get the essentials

LabKeel needs three things on the machine it watches:

| What | Why | Check you have it | You want to see |
| --- | --- | --- | --- |
| **Docker Engine** | runs LabKeel | `docker --version` | `Docker version 2…` |
| **Docker Compose plugin** | starts it with one command | `docker compose version` | `Docker Compose version v2…` or later |
| **git** | fetches the code, and updates later | `git --version` | any version |

If all three look right, skip ahead to [step 2](#2-grab-the-code). Everything else LabKeel uses
(Python, drive-health tools and so on) comes packed inside its own image.

> **Impostor alert:** if you see `Emulate Docker CLI using podman`, a `podman` version number, or
> `docker-compose` 1.x, that's Podman or an old Compose standing in for Docker. LabKeel needs the real thing:
> follow the box for your system below, which swaps the stand-ins out for you.

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
sudo docker run --rm hello-world
```

A cheerful "Hello from Docker!" means you're ready. (`--rm` tidies the test container away afterwards,
so it doesn't linger on LabKeel's Containers page with a silly random name.)

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

### Optional: ZFS, for pools of drives

ZFS joins several drives into one **pool** that keeps your data safe if a drive dies. LabKeel shows your
pools, their health, datasets and snapshots, and (if you allow it) can create and manage them.

**Do you need it?** Only if you already use ZFS, or have spare drives to pool together. A machine with one
drive has nothing to pool, so you can happily skip this: LabKeel simply hides its ZFS pages. Check whether
it's already installed with `zpool version`.

<details>
<summary><b>Ubuntu</b></summary>

ZFS is built into Ubuntu's kernel, so it's one command:

```sh
sudo apt install zfsutils-linux
```

</details>

<details>
<summary><b>Debian 13</b></summary>

Debian keeps ZFS in its `contrib` section and builds it for your kernel as it installs, which takes a few
minutes. Add the backports repository (for a newer ZFS) and tell apt to prefer it for ZFS:

```sh
sudo tee /etc/apt/sources.list.d/trixie-backports.list <<EOF
deb http://deb.debian.org/debian trixie-backports main contrib non-free-firmware
EOF
sudo tee /etc/apt/preferences.d/90_zfs <<EOF
Package: src:zfs-linux
Pin: release n=trixie-backports
Pin-Priority: 990
EOF
sudo apt update
sudo apt install linux-headers-$(dpkg --print-architecture)
sudo apt install zfs-dkms zfsutils-linux
```

For Debian 12 or Raspberry Pi OS, follow [OpenZFS's Debian guide](https://openzfs.github.io/openzfs-docs/Getting%20Started/Debian/).

</details>

<details>
<summary><b>Fedora</b></summary>

ZFS comes from the OpenZFS project's own repository and is built for your kernel:

```sh
sudo dnf install -y https://zfsonlinux.org/fedora/zfs-release-3-1$(rpm --eval "%{dist}").noarch.rpm
sudo dnf install -y kernel-devel-$(uname -r | awk -F'-' '{print $1}')
sudo dnf install -y zfs
sudo modprobe zfs
```

Fedora moves to new kernels quickly. If `modprobe zfs` fails, your kernel is newer than ZFS supports yet:
wait for a ZFS update, or boot the previous kernel. See [OpenZFS's Fedora guide](https://openzfs.github.io/openzfs-docs/Getting%20Started/Fedora/index.html).

</details>

Then, to let LabKeel create and change pools rather than just watch them, set `LABKEEL_ALLOW_ZFS_CHANGES=1` in
step 3. Its **New pool** button only offers drives that are completely unused (not your system drive, and
not USB), so it can't wipe the wrong thing.

> **Just want a look?** Make a small practice pool stored in a file. It's safe to delete afterwards:
>
> ```sh
> truncate -s 2G /var/tmp/zfs-test.img
> sudo zpool create testpool /var/tmp/zfs-test.img
> ```
>
> When you're done: `sudo zpool destroy testpool && rm /var/tmp/zfs-test.img`

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

**Choose your powers.** LabKeel starts out careful. These four are off until you switch them on:

| Setting | Set to `1` to let LabKeel… |
| --- | --- |
| `LABKEEL_ALLOW_FILE_CHANGES` | upload, edit, move and delete files |
| `LABKEEL_ALLOW_ZFS_CHANGES` | create and change ZFS pools, datasets and snapshots |
| `LABKEEL_ALLOW_RESTART` | restart and shut down the machine |
| `LABKEEL_ALLOW_APP_INSTALLS` | add apps from its catalogue, such as Jellyfin or Immich ([docs/apps.md](docs/apps.md)) |

Not sure? Leave them at `0`. You can watch everything without them, and switch them on later.

Every other setting (ports, HTTPS, reverse proxies, upload limits) is explained in
[docs/settings.md](docs/settings.md).

**Optional: switch on your services.** `config/config.json` comes with ready-made tiles for popular apps
(Plex, Jellyfin, Immich, Home Assistant, Pi-hole and friends), all switched off. Set `"enabled": true` on
the ones you run, and they'll appear on the Services page with a link and a live up/down light. There's a
blank one at the bottom for anything else. [docs/services.md](docs/services.md) has the details. Apps you
add from LabKeel's Apps page join the Services page by themselves.

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

## 8. Bring in your other machines (optional)

Got a NAS, a Raspberry Pi or a second server? Choose the arrow beside your machine's name (top left), then
**Add a server**, and LabKeel gives you one command to run on the other machine. It installs LabKeel's small
agent, which reports back here over HTTPS, and the machine joins your fleet within seconds. To see its files
on the Files page too, list its folders in `agent.local.yml` there (copy `agent.local.example.yml`). [docs/servers.md](docs/servers.md) has the details.

---

## Keeping it up to date

The easy way: **Settings → LabKeel → Check for an update**, then **Install update**. LabKeel shows what's
new, asks for your password, pulls the new version, rebuilds itself and restarts. It's away for a minute or
two. If anything goes wrong, the old version keeps running and Settings shows what happened.

It checks on its own every 6 hours too (the same schedule as container updates), and puts "update" beside
Settings in the sidebar when one is ready.

By hand, from the `labkeel` folder (this is exactly what the button does):

```sh
git pull
docker compose up -d --build
```

Your accounts, settings and history stay put. Your `docker-compose.yml` is yours too, so `git pull` leaves it
alone. After the button's update, LabKeel adds any new settings to it (switched off) and gives old names their
new ones, keeping your values and a copy of the old file. After an update by hand, **Settings → LabKeel** offers
the same. See [docs/updates.md](docs/updates.md).

Docker and git update with the rest of your system (`sudo apt upgrade`, or `sudo dnf upgrade` on Fedora).
Since Docker now comes from Docker's own repository, that keeps you on the latest version too.

---

## When things go sideways

| What's happening | What to do |
| --- | --- |
| It keeps restarting, and the log says "address already in use" | Something else is using port 8787. Add `- LABKEEL_PORT=8788` under `environment:`. |
| Browser says the connection isn't private | Expected without a trusted certificate. Continue once, or turn on Tailscale HTTPS (step 6). |
| "Emulate Docker CLI using podman", or "Error while fetching server API version" | That's Podman or an old Compose, not Docker. Install Docker as in [step 1](#1-get-the-essentials). |
| "This connection isn't encrypted" | LabKeel only signs people in over HTTPS. Use the `https://` address, remove `LABKEEL_HTTPS=off`, or behind a reverse proxy set `LABKEEL_TRUSTED_PROXY`. |
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

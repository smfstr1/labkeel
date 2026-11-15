# Apps

The **Apps** page adds a popular self-hosted app in a couple of clicks: you fill in a folder or two, and LabKeel
writes the compose file, starts it and puts it on your Services page. It's the same as doing it by hand, just
quicker, and everything it makes is ordinary Docker Compose you can manage yourself afterwards.

## Turning it on

Adding apps is off until you turn it on, because an app's containers get whatever their compose file asks for.
In LabKeel's `docker-compose.yml`:

```yaml
    environment:
      - LABKEEL_ALLOW_APP_INSTALLS=1
```

then `docker compose up -d`. Until then you can still browse the catalogue and see the compose file each app
would get.

Only admins see the Apps page. Adding or removing an app asks for your password again, and each step goes in
**Settings → Activity**. LabKeel also needs `LABKEEL_HOST_COMMANDS` left on (the default) and Docker Compose on
the machine (`docker compose version`).

## What happens when you add one

1. LabKeel writes `compose.yaml` into the app's own folder on the machine: `/srv/labkeel/apps/<name>`
   (`LABKEEL_APPS_DIR` changes where). The app's settings live in that folder too, in `config` and the like.
2. It runs `docker compose pull`, then `docker compose up -d`, in that folder.
3. It waits until the app answers on its port (up to three minutes), or for an app with no web page, until its
   containers are running.
4. It adds the app to the Services page, in an **Apps** group. This is kept in LabKeel's own data
   (`data/apps.json`): your `config.json` is never rewritten.

You can watch each step on the Apps page, and leave the page while it works. If something fails, it says which
step and why, and the app isn't listed. Whatever was already written stays in its folder, so you can look at it.

The choices you're asked for:

- **Name**: also the folder name and the container name. Adding the same app twice (a second Jellyfin for the
  children, say) just needs a different name and port.
- **Port**: the port on the machine. LabKeel won't use one something else is already listening on. A few ports
  can't be changed (Pi-hole's DNS on 53, Plex and Home Assistant, which use the machine's own network).
- **Folders**: your media, photos, downloads and so on. Folders LabKeel shares with an app read-only say so.

Folders it refuses: system folders (`/etc`, `/usr`, `/var/lib/docker` and the like), `/` and a whole top-level
folder such as `/tank` (choose `/tank/media` instead), and anything with `..` in it.

## Nextcloud

Adding Nextcloud brings everything it needs, already connected, so its first page asks only for the admin's name
and password:

| Container | What it is |
| --- | --- |
| `nextcloud` | Nextcloud itself (the official `nextcloud:apache` image) |
| `nextcloud-db` | PostgreSQL, its database. Not open to the network |
| `nextcloud-redis` | Redis, for file locking and caching. Not open to the network |
| `nextcloud-cron` | Runs Nextcloud's background jobs every five minutes |

- The database's password is made for you and kept in `.env` beside the compose file, readable only by root. It
  isn't in the compose file and the dialog never shows it. You don't need it: Nextcloud is given it directly.
- Nextcloud only works properly over HTTPS, so the dialog asks how, as for Vaultwarden (see below). It's told
  the address you'll use, which is what stops its "untrusted domain" page. Open it by that same address every
  time; the phone and desktop apps need exactly that address.
- The dialog has three folders: the app and its settings, your files, and the database. Put them together in a
  dataset of their own with **Where its data is kept**, or point the files folder at a bigger pool. Keep the
  database on a local drive, not a network share.
- If you added Nextcloud with an earlier LabKeel (the linuxserver.io image, which asked you for a database), it
  carries on working as it is. To move to this one, remove it and add it again with new, empty folders.

## ownCloud stuck on "Logging you in"

An ownCloud added with LabKeel before this fix can stop at "Logging you in", with
`identity provider temporarily unavailable` in its log. Signing in, it calls the address you open it by, and
from inside Docker that address can be out of reach. New installs are set up so that address means the container
itself. To fix an existing one, in `/srv/labkeel/apps/owncloud/compose.yaml`:

1. Set `PROXY_TLS: "true"` and `OCIS_INSECURE: "true"`. (It only lets ownCloud accept its own certificate from
   itself; your browser still gets the trusted one.)
2. Add to the `owncloud` service, with your own address:
   ```yaml
       extra_hosts:
         - "your-machine.your-tailnet.ts.net:127.0.0.1"
   ```
3. Run `sudo docker compose up -d` in that folder, then:
   ```
   sudo tailscale serve --https=9200 off
   sudo tailscale serve --bg --https=9200 https+insecure://127.0.0.1:9200
   ```

If you open it by a number (like 192.168.1.20) with a certificate made here and it still hangs, the machine's
firewall is stopping the container reaching that number: add it again using a name instead.

## Where an app's data is kept

When you add an app, **Where its data is kept** lists the places on the machine, each with its free space:

| Choice | What happens |
| --- | --- |
| LabKeel's apps folder | As before: settings and data go in the app's own folder, `/srv/labkeel/apps/<app>`. |
| A ZFS pool or dataset | Tick **Make a dataset for it** and LabKeel creates `<pool>/<app>` (you can rename it), mounted in the usual place, so the app has its own snapshots. Give a **size limit** and the app can't grow past it (a ZFS quota). Tick **Also set that much aside** to keep that space for it (a reservation). Untick the box to use a plain folder there instead. |
| Another drive | Data goes in a folder named after the app on that drive. A plain drive (ext4, XFS and so on) can't hold one folder to a size: for a limit, use a ZFS dataset. |

The app's folder boxes below follow your choice, and you can still point any one of them somewhere else (media
folders usually are). The compose file always stays in `/srv/labkeel/apps/<app>`, with the others.

- Making a dataset is a change to a pool, so it also needs `LABKEEL_ALLOW_ZFS_CHANGES=1`. Without it you can
  still keep the data in a folder on the pool.
- The limit is in the same units as the rest of LabKeel (1 GB = 1000 MB). Change it later on the pool's page,
  under Datasets.
- If the dataset can't be made (not enough room to set the space aside, say), nothing is downloaded or started.
- Removing an app keeps its dataset and everything in it. Adding the app again with the same choice uses it as
  it is. To delete the data, destroy the dataset on the pool's page.
- System folders, read-only datasets and ones that aren't mounted aren't offered.

## Secure connections (HTTPS)

Some apps are no use over plain HTTP: browsers refuse a password manager's encryption on an unencrypted page,
and phone apps won't sign in. For Vaultwarden and ownCloud the dialog asks how to secure it, and the app's own
plain web port is never opened to your network:

- **Tailscale** (the one to pick if you can). LabKeel runs `tailscale serve` on the machine, so the app is
  reached at `https://<machine>.<tailnet>.ts.net:<port>` with Tailscale's own certificate, which every device
  trusts, renewed by Tailscale. Only devices on your tailnet can reach it. Needs Tailscale 1.52 or newer, signed
  in, with HTTPS turned on on the DNS page of the Tailscale admin console (the dialog says if it isn't). The app
  listens on `127.0.0.1` only. **Remove** switches the `tailscale serve` entry off again.
- **A certificate made here.** LabKeel adds a small Caddy container (`<app>-https`) that speaks HTTPS on the
  port you chose and passes the traffic to the app, using a certificate it makes for the address you give. The
  certificate is valid for about 27 months and lives in the app's `https/certs` folder (the key is readable by
  its owner only). Browsers warn once, and phone and desktop apps usually refuse it, so use it for the web page.
  To renew it, remove the app and add it again: its folder and data are kept.

Open the app by the same address every time. ownCloud in particular is told its address when it's added, and
its apps need exactly that one.

## The catalogue

| App | What it is | Worth knowing |
| --- | --- | --- |
| Jellyfin | Films, shows and music, streamed to any device | |
| Plex | The same, with Plex's apps and sign-in | Uses the machine's own network, port 32400 |
| Immich | Your photo library, with a phone app that backs up | Uses Immich's own compose file; LabKeel writes its `.env` with a fresh database password |
| Home Assistant | Home automation | **Full control of the machine** (it asks for privileged mode, to reach USB sticks and Bluetooth), and the machine's own network |
| Sonarr, Radarr, Prowlarr | Find and fetch shows and films | |
| qBittorrent | Downloads | Its first password is in its log (Containers → qbittorrent → Logs) |
| Jellyseerr | Lets people ask for films and shows | |
| Audiobookshelf | Audiobooks and podcasts | |
| Navidrome | Your music collection, streamed | |
| Paperless-ngx | Scans your documents and makes them searchable | Brings its own Redis |
| Nextcloud | Files, calendars and contacts | Comes with its database (PostgreSQL), cache (Redis) and background jobs already connected: see below. HTTPS only |
| Syncthing | Keeps folders in step between devices | |
| Vaultwarden | Your own Bitwarden-compatible password manager | HTTPS only: see [Secure connections](#secure-connections-https) |
| ownCloud | Files synced to every device, shared by link (ownCloud Infinite Scale) | HTTPS only. Its first admin password is in its log (Containers → owncloud → Logs) |
| Uptime Kuma | Watches your sites and services | |
| Pi-hole | Blocks adverts for your whole network | Needs port 53. On Ubuntu, free it first: `DNSStubListener=no` in `/etc/systemd/resolved.conf` |
| Minecraft Bedrock | A Minecraft server for phones, consoles and Windows | You accept Minecraft's licence when you add it |

If an app's image is already running on the machine, its card says **Already running**, so you don't add a
second copy by mistake.

### Your own compose file

**Add your own compose file** takes any compose file you paste (up to 100 KB). LabKeel puts it in its own
folder, asks Docker Compose to check it first, then runs it the same way. Give it the web page port if you'd
like it on the Services page.

## Afterwards

Each added app is listed at the top of the Apps page:

- **Open** goes to its web page.
- **Details** shows its folder and compose file, and **Remove**.
- **Update**, when the Containers page has found a newer image for it, opens the container's update panel.

**Remove** runs `docker compose down` and takes the app off the Services page. Its folder, settings and data
stay where they are: delete the folder yourself if you're sure. Adding it again with the same name picks up
where it left off.

To change something LabKeel didn't ask about, edit `compose.yaml` in the app's folder and run
`docker compose up -d` there, as you would for any compose project.

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
| Nextcloud | Files, calendars and contacts | Its own self-signed certificate on its port |
| Syncthing | Keeps folders in step between devices | |
| Vaultwarden | Password manager (works with the Bitwarden apps) | Browsers only let it work over HTTPS: reach it through a reverse proxy or Tailscale |
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

# Services

The **Services** page is your homelab's front door: one tile for everything you run, each with a link
and a live up/down light. They're set in `config/config.json`.

## Turning on the ready-made ones

Your `config.json` starts with examples for popular apps: Plex, Jellyfin, Immich, the *arr family,
Home Assistant, Pi-hole, Minecraft and more. They're all switched off, so a fresh install doesn't open on
a wall of red. For each one you run, change

```json
"enabled": false
```

to

```json
"enabled": true
```

Save, and the tile appears within a minute (or press **Check now**). No restart needed.

Running something on a different port? Change the number in both `"url"` and `"check"`. Don't run it at
all? Leave it switched off, or delete it.

## Adding your own

Copy the **My app** example at the bottom, or start from this:

```json
{ "name": "Recipes", "group": "Home", "description": "What's for dinner",
  "url": "http://{host}:9925" }
```

That's a complete service: a name, and a link that's also checked to see if it's up.
Leave out `"enabled"` and it's on.

## Every field

| Field | What it does |
| --- | --- |
| `name` | The tile's name. Required. You can also rename tiles on the page itself (that's only shown on the page). |
| `enabled` | `false` hides it and stops checking it. Leave it out, or set `true`, to show it. |
| `url` | The link the tile opens. Leave it out for something with no web page, like a game server. |
| `group` | Tiles with the same group sit together under that heading. |
| `description` | A short line under the name. |
| `icon` | A letter or emoji for the tile. Leave it out to use the name's first letter. |
| `logo` | An image file in `config/logos` to show instead (`"logo": "my-app.png"`). Without it, LabKeel looks for one named after the service: `Home Assistant` → `config/logos/home-assistant.svg` (or `.png`, `.webp`, `.jpg`). Up to 2 MB. |
| `container` | The Docker container it runs in. The tile shows down if that container stops, even before anything else notices. |
| `check` | How to tell it's up. Leave it out to check `url`, or, if you've set `container` instead, just to watch the container. See below. |

## Placeholders

| Write | It becomes |
| --- | --- |
| `{host}` | In links: the address you opened LabKeel at, so the same link works from your laptop over Wi-Fi or your phone over Tailscale. In checks: this machine itself. |
| `{gateway}` | Your router's address, found automatically. |

`{host}` only fits apps on the same machine as LabKeel. For anything elsewhere, write its full address, like
`http://192.168.1.20:8096`. If you reach your apps through a reverse proxy with their own names, like
`https://jellyfin.example.com`, write those in `url` and keep `{host}` in `check`.

## Checks

| Check | Example | It's up when… |
| --- | --- | --- |
| Web page (the default) | `{ "type": "http", "url": "http://{host}:8096/health" }` | the page answers with anything below an error 500. A login page counts. |
| Port open | `{ "type": "tcp", "host": "{host}", "port": 25565 }` | something accepts a connection on that port |
| Minecraft Bedrock | `{ "type": "bedrock", "port": 19132 }` | the server answers, and the tile shows players online |
| Container only | `{ "type": "none" }` | its `container` is running |

Checks accept a `"timeout"` in seconds (6 for web pages and 3 for ports, unless you say otherwise).
Web checks don't verify certificates, so self-signed HTTPS is fine: they only ask "is it there?".

## When a tile won't go green

- **"connection refused"**: nothing is listening on that port here. Check the port, and that the app is running.
- **"no reply"**: something's in the way, like a firewall, or the app only listens on another address.
- **"No container named …"**: the `container` name doesn't match. `docker ps` shows the real names.
- **"HTTP 502"** or similar: the app is there but unwell. Its own logs will say why.

<p align="center">
  <img src="images/labkeel-logo.png" alt="LabKeel logo" width="180">
</p>

<h1 align="center">LabKeel</h1>

<p align="center">
  A self-hosted dashboard for your home lab: system stats, Docker containers, ZFS pools,<br>
  drive health, files and service checks in one page.
</p>

Simple, lightweight, and open-source, LabKeel is a hardware and docker dashboard, created so users can enjoy home-labbing, maintain hardware, and keep information local and secure all through a GUI.

> [!IMPORTANT]
> LabKeel is in early stages of development, and isn't ready for
> general use yet. Expect breaking changes until version 1.0.

## What it does, out the box

- **Overview**: CPU, memory, network, temperatures, GPU and power, with history
- **Containers**: see, start, stop and restart Docker containers, and read their logs
- **Storage**: ZFS pools, datasets and snapshots, plus disk space for every filesystem
- **Drive health**: SMART readings, self-tests and warnings before a drive fails
- **Files**: browse, upload, edit, move and delete files, with a trash you can restore from
- **Services**: links to everything you run, each with a live up/down check
- **Alerts**: to ntfy or Discord when something needs attention
- **Accounts**: admins and view-only accounts, passwords or passkeys, over HTTPS by default

## Information

LabKeel can restart your server, change ZFS pools and edit files. It needs privileged access
to the machine to do that, so those features start switched off; turn on only the ones you need.
Keep it off the open internet, and only make admin accounts for people you'd trust with full
control of the server. See [SECURITY.md](SECURITY.md) for how it's protected.

## Screenshots

![Containers grouped by compose project, with an Update badge on the ones with a newer image](images/containers.png)
**Containers**: grouped by project, with update checks

>

![A container's logs, with how to update it](images/container-logs.png)
**Logs**: live, searchable, with the update command

>

![The file browser in a Movies folder](images/files.png)
**Files**: browse, upload, move, delete

>

![Service tiles with live up/down checks](images/services.png)
**Services**: every app, with a live check

>

![Settings showing a LabKeel update ready to install](images/labkeel-update.png)
**Updates itself** from Settings

>

![The activity log, naming who did what](images/activity.png)
**Activity log**: who did what, and when

<p align="center"><img src="images/phone.png" alt="LabKeel on a phone" width="300"></p>

The screenshots show a demo machine with sample containers, files and history.

## Install

You'll need a Linux machine with Docker. The short version:

```sh
git clone https://github.com/smfstr1/labkeel.git && cd labkeel
cp docker-compose.example.yml docker-compose.yml
mkdir -p config && cp config.example.json config/config.json
docker compose up -d --build
docker logs labkeel        # this is your one-time lab set-up code
```

Then open `https://<your server's address>:8787`. The friendly, step-by-step version, including
updating and uninstalling, is in **[INSTALL.md](INSTALL.md)**.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest              # tests
python -m pyflakes app tests  # mistakes such as unused or undefined names
```

The tests never touch the real machine: they run against a throwaway folder with host
commands switched off.

## Licence

See [LICENSE](LICENSE). The bundled fonts, Hanken Grotesk and JetBrains Mono, are under the
SIL Open Font License; their licences are in `app/static/fonts`.

'**Lab**' is what it's supporting, '**Keel**' like the Keel of a ship! (Google it.)

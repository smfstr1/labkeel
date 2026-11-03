# LabKeel

A self-hosted dashboard for your home server: system stats, Docker containers, ZFS pools,
drive health, files and service checks in one page.

> [!IMPORTANT]
> LabKeel is currently set to v0.0.1b, and isn't ready for
> general use yet. Expect breaking changes until version 1.0.

## What it does

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

## Try it

You'll need Docker with Compose on a Linux machine.

```sh
git clone https://github.com/labkeel/labkeel.git
cd labkeel
cp docker-compose.example.yml docker-compose.yml
mkdir -p config && cp config.example.json config/config.json
```

Edit `docker-compose.yml`: choose the folders to show under `/browse`, turn on the features you
want, and replace the `image:` line with `build: .`. Then:

```sh
docker compose up -d --build
docker logs labkeel        # shows a one-time set-up code
```

Open `https://<your server's address>:8787`, enter the set-up code, and create your admin account.
Add more accounts, admin or view-only, from **Account** in the sidebar.

Forgot your password? On the server: `docker exec -it labkeel python accounts.py passwd <name>`.

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

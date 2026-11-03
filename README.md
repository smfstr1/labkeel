# LabKeel

A self-hosted dashboard for your home server: system stats, Docker containers, ZFS pools,
drive health, files and service checks in one page.

> **Status: in development.** LabKeel is in early stages of development, and isn't ready for
> general use yet. Expect breaking changes until version 1.0.

> [!Current Version]
> LabKeel is currently in version 0.0.1a

## What it does

- **Overview**: CPU, memory, network, temperatures, GPU and power, with history
- **Containers**: see, start, stop and restart Docker containers, and read their logs
- **Storage**: ZFS pools, datasets and snapshots, plus disk space for every filesystem
- **Drive health**: SMART readings, self-tests and warnings before a drive fails
- **Files**: browse, upload, edit, move and delete files, with a trash you can restore from
- **Services**: links to everything you run, each with a live up/down check
- **Alerts**: to ntfy or Discord when something needs attention
- **Sign-in**: password or passkey, over HTTPS by default

## Important

LabKeel can restart your server, change ZFS pools and edit files. It needs privileged access
to the machine to do that. Keep it off the open internet, use a long password, and only give
the password to people you'd trust with full control of the server.

## Try it

You'll need Docker with Compose on a Linux machine.

```sh
git clone https://github.com/labkeel/labkeel.git
cd labkeel
cp docker-compose.example.yml docker-compose.yml
mkdir -p config && cp config.example.json config/config.json
```

Edit `docker-compose.yml`: set `DASH_USER` and `DASH_PASS`, choose the folders to show under
`/browse`, and replace the `image:` line with `build: .`. Then:

```sh
docker compose up -d --build
```

Open `https://<your server's address>:8787` and sign in.

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

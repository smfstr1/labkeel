# Container update checks

LabKeel can tell you when a newer image is out for a container you run. It only looks: it never pulls an
image or re-creates a container, so nothing changes until you decide to update.

## What you see

- An **Update** badge next to each container that's behind, and an **Updates** filter on the Containers page.
- A line under the list saying when images were last checked.
- In a container's panel, how to update it. For a compose project:
  `docker compose pull <service> && docker compose up -d <service>`, run in the project's folder.
  If you've already pulled the new image but not re-created the container, the panel says so.
- Optionally, an alert (ntfy or Discord) when new versions turn up: **Settings → Alerts → Newer images are out
  for your containers**. You're told once per new version, not on every check.

## How it works

For each image a container uses, LabKeel asks the image's registry which digest its tag points to now, and
compares that with the digest your container's image was pulled as.

- One `HEAD` request per image (plus a token request where the registry asks for one). Docker Hub doesn't
  count `HEAD` requests towards its pull limit.
- Requests are anonymous: LabKeel never sends a password or your Docker credentials anywhere.
- At most 4 registries are asked at once, and each answer has 15 seconds.
- Results are kept in `data/labkeel.db`, so they survive restarts.

It runs every 6 hours by default (**Settings → Container updates**: every 1, 3, 6, 12 or 24 hours, or off),
and whenever an admin presses **Check for updates**. Viewers see the results but can't start a check.

## What each status means

| Status | Meaning |
|---|---|
| Current | The container runs what the tag points to now. |
| Update | The tag points to a newer image than the container runs. |
| Pinned | The container names an exact digest (`image@sha256:…`), so it never changes by itself. |
| Local | The image was built or loaded on this machine, not pulled, so there's nothing to compare with. |
| Unknown | The registry won't show the image without a login: it's private, or it only exists on this machine. |
| Error | The check failed: the registry was down, the tag was deleted, or it asked LabKeel to slow down. The next check tries again. |

## Registries

Docker Hub, GitHub (`ghcr.io`), LinuxServer (`lscr.io`, which sends requests on to GitHub), Quay, and any
registry that follows the OCI distribution spec and lets anyone read public images. Registries are only
contacted over HTTPS.

Private images aren't checked yet: they'd need LabKeel to hold your registry password, which it doesn't.

## Updating LabKeel itself

**Settings → LabKeel** shows the version you're running and checks for a newer one on the same schedule:

- **Installed from source** (`build: .`, as in INSTALL.md): the new commits on the branch your copy follows,
  found with `git fetch`. Git runs as the folder's owner, with their own sign-in, so a private repository works.
- **Installed from an image**: a newer image for the tag you run, checked as above.

**Install update** asks for your password, then hands the update to systemd on the host, because LabKeel can't
replace its own container from inside it. It runs exactly the commands you'd type:

```sh
git pull --ff-only                       # source installs
docker compose up -d --build labkeel
# or, for image installs:
docker compose pull labkeel && docker compose up -d labkeel
```

Progress goes to `data/update.log`, and the script that ran is kept in `data/update.sh`. If anything fails,
the old version keeps running, you get an alert, and Settings shows the output.

It won't update over local edits to LabKeel's files, or a copy with commits of its own: git would refuse,
so Settings says so first. It needs the set-up from INSTALL.md (Docker Compose, `privileged`, `pid: host`) and a
host that runs systemd; otherwise Settings shows the commands to run yourself.

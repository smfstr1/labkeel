# What's new in LabKeel

## LabKeel updates itself
- **Settings → LabKeel** shows your version and checks for a new one: new commits on your branch, or a newer image.
- **Install update** asks for your password, then pulls, rebuilds and restarts. It's away for a minute or two.
- If anything fails, the old version keeps running and Settings shows what happened.
- It checks every 6 hours on its own, and shows "update" beside Settings when one is ready.

## Container update checks
- Finds newer images on Docker Hub, GHCR, lscr.io, Quay and other registries.
- **Update** badges, an **Updates** filter, and the exact command to update each container.
- Checks every 6 hours or on demand, with an optional alert. Nothing is downloaded or changed for you.

## More container controls
- Pause, resume and remove (asks for your password; volumes are kept).
- Select several containers and act on them all at once.
- **Needs attention** filter, and log search with "last 15 minutes / hour / day".

## Activity log
- Names who did each thing, and from where, including work that finishes in the background.
- Stored in a database and searchable.

## Under the hood
- Python 3.13 on Debian 13. Container logs capped at 3 × 10 MB in the example compose file.
- CI tests Python 3.12 and 3.13 against real Docker, scans for risky code and vulnerable dependencies, and checks that the built image starts.
- Dependabot suggests dependency updates weekly.

## Fixes
- Update checks now see every name an image has (affected Docker's containerd image store).
- The per-thread CPU bars no longer stretch across the page on machines with few threads.
- The container list updates straight away after you start or stop something.
- Restarts no longer wait for Plex or Minecraft unless you turn that on.

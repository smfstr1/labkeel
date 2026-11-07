# Several servers

One LabKeel can show your other machines too: a NAS, a Raspberry Pi, a second server. Each runs LabKeel's small
**agent**, which reports to your main LabKeel (the **hub**) over HTTPS. You see them all on the **Servers**
page, and each has its own page with its figures, containers, storage and drive health. Their files are on
the **Files** page, next to this machine's.

Other machines' containers and settings are **shown, not changed**. Their files can be changed only where you
allow it on that machine (see [Files](#files-on-your-other-machines)). Starting, stopping and updating their
containers from the hub is planned, and will be off by default like LabKeel's other powerful features.

## Adding a machine

1. On your main LabKeel, choose the arrow beside the machine's name (top left), then **Add a server**. Once
   you have more than one machine, the **Servers** page has the same button. Give it a name, and check the
   address the other machine will reach LabKeel on: its Tailscale name works well. LabKeel asks for your
   password again.
2. It shows a command with a one-time **join code**. The code works once, for 15 minutes.
3. On the other machine, with Docker and Git installed, run that command. It looks like this:

   ```sh
   [ -d labkeel-agent ] || git clone https://github.com/smfstr1/labkeel.git labkeel-agent
   cd labkeel-agent && LABKEEL_HUB=https://homelab.tail1234.ts.net:8787 LABKEEL_JOIN_CODE=K7Q-4MX-92P \
     LABKEEL_HUB_KEY=sha256:… docker compose -f agent.compose.yml up -d --build
   ```

4. Within a few seconds the dialog says it has joined, and the machine appears on the Servers page.

The command is safe to run again on a machine that already has the agent: it skips the download.

Once it has joined, the code isn't needed again: the agent keeps its own token in `agent-data` beside
`agent.compose.yml`, and starts again by itself after a restart.

The agent needs no open port on the other machine: it only connects out, to the hub.

## How it stays safe

- **Only your LabKeel.** The command includes your LabKeel's certificate key (`LABKEEL_HUB_KEY`). Before
  sending anything, the agent checks that it's talking to a LabKeel with that key, or one with a certificate
  your machine already trusts (a Tailscale or Let's Encrypt one). That works even with LabKeel's own
  self-signed certificate, which LabKeel keeps the same key for when it renews it.
- **Only machines you added.** Joining takes a code that works once, for 15 minutes, made by an admin who gave
  their password again. After that, each agent has its own token. The hub keeps only a hash of it, the same
  way it keeps passwords.
- **Only files, and only where you allow it.** The hub can ask an agent for one thing beyond its readings: the
  same file requests the Files page makes here, from a fixed list, in the folders mounted under `/browse` on that
  machine. They change nothing unless `LABKEEL_ALLOW_FILE_CHANGES=1` is set there. Nothing can reach its
  containers, its settings or the rest of the machine.
- Wrong codes are counted like wrong passwords, but separately, so a machine trying an old code can't stop
  anyone signing in.

## What you see

- **Servers**: a card for each machine with its CPU, memory and disk, how many containers are running, its
  power draw where it can be read, and anything wrong. Totals across them all appear once there's more than
  one.
- **A machine's page**: its figures, every container, filesystems, ZFS pools and drive health.
- **Containers → All servers**: every container on every machine in one list.
- **Alerts**: **Settings → Alerts → Another server you've added stops reporting, or has a problem** sends an
  alert (ntfy or Discord) when a machine hasn't reported for five minutes, or has a container that has
  crashed or keeps restarting, a full disk, or a pool or drive in trouble, using the same limits as for this
  machine.

A machine shows as offline after a minute without a report. Viewers can see the Servers page; only admins can
add, rename or remove machines.

## Files on your other machines

Each machine's shared folders appear on the **Files** page as a group of their own, under the machine's name,
next to this machine's. Browse, preview, download, search, and (where it's allowed) upload, rename, delete and
use the Bin there, just as here.

### Sharing a machine's folders

On that machine, in the `labkeel-agent` folder:

1. Copy `agent.local.example.yml` to `agent.local.yml` (git leaves that file alone, so updating the agent never
   touches it).
2. List the folders to share under `volumes:`, each mounted under `/browse`. Add `:ro` to keep one read only.
3. To change files from LabKeel too, set `LABKEEL_ALLOW_FILE_CHANGES=1` there.
4. Run the command from your LabKeel again, or `docker compose -f agent.compose.yml -f agent.local.yml up -d`.

```yaml
services:
  labkeel-agent:
    environment:
      - LABKEEL_ALLOW_FILE_CHANGES=1
    volumes:
      - /mnt/tank/media:/browse/media
      - /mnt/tank/archive:/browse/archive:ro
```

The machine's page on **Servers** lists what it shares, and whether LabKeel can change it.

### Copying and moving between machines

- **Copy to another server…**: pick files or folders, then the machine and folder to copy them to.
- **Two panes**: the switch at the top of Files puts a second folder beside the first, each on whichever
  machine you choose. Copy or move between them with the buttons, or by dragging across (hold Shift to move).
- Dragging onto another machine's folder in the tree, or copying (Ctrl+C) here and pasting (Ctrl+V) there, works
  too.

A copy between machines runs in the background, with its progress at the top of every page, one at a time:

- Before it starts, LabKeel checks the target allows changes and has room.
- If something there has the same name, you choose: **keep both** (the copy is called "name (2)"), **replace
  it** (the old one goes to that machine's Bin) or **skip it**.
- Each file keeps its date, and is **checked once it lands**: its SHA-256 there must match what was read here.
- A **move** deletes the originals, to their machine's Bin, only once everything has been copied and checked,
  and never things that were skipped. Stopping a copy part-way deletes nothing.

Copies go through this LabKeel, encrypted both ways, so the two machines don't need to reach each other.

### Searching every machine

Choose **On every machine** beside **Search**: each machine looks through all its shared folders, and the
results come back in a group for each.

### How it's kept safe

- Only admins can use Files on other machines, as on this one.
- Where Files here asks for your password again, it's asked here, and your password is never sent to the other
  machine.
- The agent only answers the file requests on a fixed list, with LabKeel's own file code and its checks on every
  path (nothing outside the shared folders, no links leading out, LabKeel's own folder hidden).
- Every change is in that machine's activity log, and in this one's as "On nas: …", with who made it.
- Whatever the other machine sends back is kept in its place: a file it sends can't run as a page here.

## Removing a machine

On its page, choose **Remove** (LabKeel asks for your password again). Its token stops working straight away.
Then, on that machine:

```sh
cd labkeel-agent && docker compose -f agent.compose.yml down
```

(Its shared folders aren't touched.)

and delete the `labkeel-agent` folder if you're done with it.

## If something goes wrong

The agent's log says what's happening: `docker logs labkeel-agent`.

- **"The hub's certificate isn't trusted and its key isn't the one this agent was given"**: the hub's
  certificate changed to one with a new key that isn't otherwise trusted (you swapped in your own, say). Make a
  new code on the hub, then run the command again on the machine (it joins afresh).
- **"refused"**: the machine was removed on the hub, or the code was used or ran out. Make a new code and run
  the command again.
- **It joined, then went offline**: the machine can't reach the hub's address. Try `curl -k <address>` from it.

Updating the agent: in the `labkeel-agent` folder, `git pull`, then `docker compose -f agent.compose.yml up -d --build`.

# Several servers

One LabKeel can show your other machines too: a NAS, a Raspberry Pi, a second server. Each runs LabKeel's small
**agent**, which reports to your main LabKeel (the **hub**) over HTTPS. You see them all on the **Servers**
page, and each has its own page with its figures, containers, storage and drive health.

For now other machines are **shown, not changed**: the agent only reads. Starting, stopping and updating their
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
- **Only reading.** The agent sends readings and nothing else, and nothing the hub sends back can make it change
  anything.
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

## Removing a machine

On its page, choose **Remove** (LabKeel asks for your password again). Its token stops working straight away.
Then, on that machine:

```sh
cd labkeel-agent && docker compose -f agent.compose.yml down
```

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

# Clusters

The **Clusters** page is for machines you look after together. There are two kinds, and each works without the
other. Kubernetes isn't here yet: the page shows it as "later".

| | Group of servers | Docker Swarm |
| --- | --- | --- |
| What it is | A name for machines LabKeel already knows (this one and its agents) | Docker's own clustering: services run as copies across machines |
| Shows | Each machine's load, containers and problems, side by side | Nodes, services, where each copy runs, and whether the managers can still agree |
| Changes anything? | No. Nothing is installed or changed on the machines | Only when you turn it on (below) |
| Who can make one | Any admin | Any admin, once `LABKEEL_ALLOW_CLUSTER_CHANGES=1` |
| Who can look | Everyone who can sign in | Everyone who can sign in |

## A group of servers

**New cluster → Group of servers**, give it a name, and tick the machines. That's all it does: it lets you see
them together, with the same checks as the alerts (a machine that stopped reporting, a container that keeps
restarting, a full disk, a pool that isn't online). Deleting a group, or removing a server from LabKeel, leaves
the machines as they are.

## Docker Swarm

A Swarm needs Docker on every machine and LabKeel running on a **manager**. LabKeel shows the Swarm that the
Docker on its own machine belongs to. If this machine is only a worker, the page says so and shows no cluster:
open LabKeel on a manager.

The page shows:

- **Nodes**: each machine's role (manager or worker, and which manager leads), whether it is ready, down or
  drained, its load (when it also runs LabKeel's agent, matched by name), and how many tasks it has.
- **Can it survive a failure?** A Swarm needs more than half its managers to agree. With three managers, one can
  fail; with two, losing either stops changes, and with one there is no spare at all. Running services keep
  running when changes stop. A Raspberry Pi makes a fine third manager.
- **Services**: image, how many copies are wanted and running, and where they run. A service pinned to one
  machine says so.
- **Events**: tasks that failed or were rejected recently, with Docker's reason. Swarm keeps only the last few
  of each, so old failures are forgotten.

### Turning on changes

Changing a Swarm is off until you turn it on. In LabKeel's `docker-compose.yml`:

```yaml
    environment:
      - LABKEEL_ALLOW_CLUSTER_CHANGES=1
```

then `docker compose up -d`. Everything on the page still works for looking. With it on, admins can:

- **Start a Swarm** on this machine (New cluster → Docker Swarm). If the machine has more than one address,
  Docker asks which one other machines should use: type it under Address.
- **Join a machine**: shows the `docker swarm join` command to run there, as a worker or a manager.
- **Drain a machine** before updates or a reboot (and make it active again). LabKeel first shows what will move
  and what can't: a service pinned to that machine stops until it's active again.
- **Remove a machine** that is down.
- **Add a service** from an image, with copies (or one on every machine) and an optional published port.
- **Scale**, **restart** and **remove** a service.
- **Leave the Swarm** on this machine.

Each of these asks for your password again (once per ten minutes on the same device), is admin-only, and is
written to **Settings → Activity** with your name. LabKeel uses the Docker it already has: it opens no port,
installs nothing, and never stores or logs a join token.

A service added here gets no folders from the machine and no environment variables, and Swarm services can't be
privileged. For anything more, write a compose file and add it by hand with `docker stack deploy`; LabKeel will
show it.

### Before you build one

- **Ports between machines.** Swarm needs 2377/tcp to reach a manager, 7946 (tcp and udp) and 4789/udp between
  machines. A firewall in the way is the usual reason a machine won't join.
- **Data doesn't move by itself.** Plex, Immich and your files live on one machine's ZFS pools, so they stay
  ordinary containers. A Swarm suits small, stateless things: DNS, dashboards, proxies, status pages.
- **Don't drain LabKeel's machine thinking it moves LabKeel.** LabKeel runs as an ordinary container, not a
  Swarm service, so a Swarm doesn't move it.
- **Docker's name for every Swarm is "default".** The name on the page is only how LabKeel shows it
  (Settings tab → Rename).

### What isn't there yet

Kubernetes (k3s) is planned: read-only first, from a limited access token. Alerts for a Swarm (a node down, a
service below its copies) aren't wired into the alert settings yet.

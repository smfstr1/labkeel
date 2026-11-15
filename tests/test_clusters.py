"""The Clusters page (clusters.py): groups of servers, and Docker Swarm shown and changed through a stand-in Docker
(a real Swarm isn't started by the tests: it would take over the machine's Docker)."""
import time
from types import SimpleNamespace

import pytest

import clusters
import containers
import servers
from conftest import PASSWORD, sign_in
from test_security import STRONG, _add

NAS, PI = "a" * 12, "b" * 12
SAM_ID, NAS_ID, PI_ID = "samnode0000000000000000aa", "nasnode0000000000000000bb", "pinode00000000000000000cc"
CADDY, PIHOLE, VAULT, HOME = "svccaddy000000000000000aa", "svcpihole00000000000000bb", "svcvault0000000000000000cc", "svchome00000000000000000dd"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    servers._save({"servers": {}, "codes": {}})
    servers._reports.clear()
    clusters._save({"groups": {}})
    clusters._changed()
    monkeypatch.setattr(clusters, "ALLOWED", True)
    yield
    servers._save({"servers": {}, "codes": {}})
    clusters._save({"groups": {}})
    clusters._changed()


def _agents(**states):
    """Agents that have joined: name -> how long ago they last reported."""
    d = {}
    for sid, (name, ago) in {NAS: ("nas", states.get("nas", 1)), PI: ("pi5", states.get("pi5", 1))}.items():
        d[sid] = {"name": name, "joined": 1, "last_seen": time.time() - ago,
                  "last": {"host": {"hostname": name}, "system": {"cpu": {"percent": 4}, "memory": {"percent": 22}},
                           "containers": [{"name": "a", "state": "running"}, {"name": "b", "state": "running"}]}}
    servers._save({"servers": d, "codes": {}})


def logged(client, part, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        found = client.get("/api/events", params={"q": part}).json()
        if found:
            return found[0]
        time.sleep(0.05)
    raise AssertionError(f"nothing in the activity log about {part!r}")


# --------------------------------------------------------------------------- groups
def test_make_change_and_delete_a_group(client):
    _agents()
    r = client.post("/api/clusters/groups", json={"name": "Home", "servers": ["this", NAS]})
    assert r.status_code == 200, r.text
    gid = r.json()["id"]
    cl = client.get("/api/clusters").json()
    home = next(c for c in cl["clusters"] if c["id"] == gid)
    assert home["kind"] == "group" and home["name"] == "Home" and home["status"] == "healthy"
    assert [n["name"] for n in home["nodes"]][1:] == ["nas"] and home["containers"]["total"] >= 2
    assert cl["tiles"]["healthy"] == 1 and cl["tiles"]["machines"] == 2 and cl["kubernetes"] == "later"
    assert logged(client, "Made the cluster Home")["kind"] == "settings"

    assert client.post(f"/api/clusters/groups/{gid}", json={"name": "Lab", "servers": [NAS, PI]}).status_code == 200
    one = client.get(f"/api/clusters/{gid}").json()
    assert one["name"] == "Lab" and one["members"] == [NAS, PI] and [m["name"] for m in one["machines"]][0:1]

    assert client.post(f"/api/clusters/groups/{gid}/delete").status_code == 200
    assert client.get(f"/api/clusters/{gid}").status_code == 404
    assert "untouched" in logged(client, "Deleted the cluster Lab")["text"]


def test_a_group_must_be_named_properly_and_hold_known_machines(client):
    _agents()
    for bad in ("", " ", "-x", "a" * 41, "x/y", "<b>"):
        assert client.post("/api/clusters/groups", json={"name": bad, "servers": []}).status_code == 400, bad
    assert client.post("/api/clusters/groups", json={"name": "A", "servers": ["c" * 12]}).status_code == 400
    assert client.post("/api/clusters/groups", json={"name": "A", "servers": "this"}).status_code == 400
    assert client.post("/api/clusters/groups", json={"name": "A", "servers": [1]}).status_code == 400
    assert client.post("/api/clusters/groups", json={"name": "A", "servers": ["this", "this", NAS]}).status_code == 200
    assert client.get("/api/clusters").json()["clusters"][0]["nodes"][0]["id"] == "this"   # listed once
    assert len(client.get("/api/clusters").json()["clusters"][0]["nodes"]) == 2
    assert client.post("/api/clusters/groups", json={"name": "a", "servers": []}).status_code == 409   # same name


def test_there_is_a_limit_to_groups(client, monkeypatch):
    monkeypatch.setattr(clusters, "MAX_GROUPS", 2)
    for n in "AB":
        assert client.post("/api/clusters/groups", json={"name": n, "servers": []}).status_code == 200
    assert client.post("/api/clusters/groups", json={"name": "C", "servers": []}).status_code == 409


def test_a_group_with_a_silent_machine_needs_attention(client):
    _agents(pi5=3600)
    gid = client.post("/api/clusters/groups", json={"name": "Home", "servers": ["this", NAS, PI]}).json()["id"]
    c = client.get("/api/clusters").json()
    home = next(x for x in c["clusters"] if x["id"] == gid)
    assert home["status"] == "attention" and home["note"] == "pi5 isn't reporting."
    assert c["tiles"] == {"healthy": 0, "attention": 1, "machines": 3, "online": 2, "services": None}
    assert logged(client, "Made the cluster")   # (and the page needn't wait for Docker to say anything)


def test_an_empty_group_is_not_called_healthy(client):
    gid = client.post("/api/clusters/groups", json={"name": "Later", "servers": []}).json()["id"]
    home = next(x for x in client.get("/api/clusters").json()["clusters"] if x["id"] == gid)
    assert home["status"] == "empty" and "No machines" in home["note"]


def test_a_removed_server_leaves_its_groups(client):
    _agents()
    gid = client.post("/api/clusters/groups", json={"name": "Home", "servers": ["this", NAS]}).json()["id"]
    assert client.post(f"/api/servers/{NAS}/remove", json={"password": PASSWORD}).status_code == 200
    assert client.get(f"/api/clusters/{gid}").json()["members"] == ["this"]


def test_a_damaged_clusters_file_is_survived(client):
    clusters.U()._write("clusters.json", {"groups": {"a" * 12: {"name": "Odd", "servers": [3, None, "this"]}, "b" * 12: 7}, "swarm_name": 5})
    c = client.get("/api/clusters")
    assert c.status_code == 200
    assert [x["name"] for x in c.json()["clusters"] if x["kind"] == "group"] == ["Odd"]
    assert clusters.swarm_name() == "Docker Swarm"


def test_viewers_can_look_but_not_change(users, client):
    _agents()
    gid = client.post("/api/clusters/groups", json={"name": "Home", "servers": ["this", NAS]}).json()["id"]
    _add("vera")
    v = sign_in("vera", STRONG)
    assert v.get("/api/clusters").status_code == 200 and v.get(f"/api/clusters/{gid}").status_code == 200
    assert v.post("/api/clusters/groups", json={"name": "X", "servers": []}).status_code == 403
    assert v.get("/api/clusters/swarm/nodes/" + SAM_ID + "/preview").status_code == 403   # an admin's question


def test_group_problems_are_listed(client):
    _agents()
    d = servers._load()
    d["servers"][NAS]["last"]["containers"] = [{"name": "db", "state": "exited", "exit_code": 1}]
    servers._save(d)
    gid = client.post("/api/clusters/groups", json={"name": "Home", "servers": ["this", NAS]}).json()["id"]
    ev = client.get(f"/api/clusters/{gid}").json()["events"]
    assert ev and "db on nas" in ev[0]["title"] and ev[0]["level"] == "bad"


# --------------------------------------------------------------------------- Swarm: a stand-in Docker
def node(nid, name, role="manager", state="ready", avail="active", leader=False, reach="reachable",
         updated="2026-11-13T10:00:00.123456789Z"):
    a = {"ID": nid, "Version": {"Index": 5}, "UpdatedAt": updated,
         "Spec": {"Role": role, "Availability": avail, "Labels": {}},
         "Description": {"Hostname": name, "Engine": {"EngineVersion": "27.3.1"}},
         "Status": {"State": state, "Addr": "192.168.1.10"}}
    if role == "manager":
        a["ManagerStatus"] = {"Reachability": reach, **({"Leader": True} if leader else {})}
    return a


def service(sid, name, image, mode=None, constraints=(), ports=()):
    return {"ID": sid, "Spec": {"Name": name, "Mode": mode or {"Replicated": {"Replicas": 1}},
                                "TaskTemplate": {"ContainerSpec": {"Image": image},
                                                 "Placement": {"Constraints": list(constraints)}}},
            "Endpoint": {"Ports": [{"Protocol": "tcp", "TargetPort": t, "PublishedPort": p} for p, t in ports]}}


def task(svc, nid, state="running", desired="running", err=None, when="2026-11-13T11:00:00Z"):
    return {"ID": f"{svc}{nid}{state}"[:25], "ServiceID": svc, "NodeID": nid, "DesiredState": desired,
            "Status": {"State": state, "Timestamp": when, **({"Err": err} if err else {})}}


NODES = [node(SAM_ID, "sam-server", leader=True), node(NAS_ID, "nas"), node(PI_ID, "pi5", state="down", reach="unreachable")]
SERVICES = [service(CADDY, "caddy", "caddy:2-alpine@sha256:" + "0" * 64, {"Global": {}}),
            service(PIHOLE, "pihole", "pihole/pihole:latest", constraints=["node.hostname == nas"], ports=[(8053, 80)]),
            service(VAULT, "vaultwarden", "vaultwarden/server:latest", constraints=["node.hostname == sam-server"]),
            service(HOME, "homepage", "ghcr.io/gethomepage/homepage", {"Replicated": {"Replicas": 2}})]
TASKS = [task(CADDY, SAM_ID), task(CADDY, NAS_ID), task(PIHOLE, NAS_ID), task(VAULT, SAM_ID),
         task(HOME, SAM_ID), task(HOME, NAS_ID),
         task(HOME, PI_ID, "failed", "shutdown", err="task: non-zero exit (1)", when="2026-11-13T09:30:00Z")]


class Thing:
    """A Docker object: its attributes, and a record of what was done to it."""

    def __init__(self, fake, attrs, kind):
        self.fake, self.attrs, self.kind, self.id = fake, attrs, kind, attrs["ID"]

    def update(self, spec):
        self.fake.calls.append((f"{self.kind}.update", self.id, spec))

    def remove(self, force=False):
        self.fake.calls.append((f"{self.kind}.remove", self.id, force))

    def scale(self, n):
        self.fake.calls.append(("service.scale", self.id, n))

    def force_update(self):
        self.fake.calls.append(("service.force_update", self.id))


class Docker:
    def __init__(self, local="active", control=True, nodes=NODES, services=SERVICES, tasks=TASKS):
        self.calls, self.local, self.control = [], local, control
        self._n, self._s, self._t = nodes, services, tasks
        self.refuse = None
        self.nodes = SimpleNamespace(list=lambda: [Thing(self, a, "node") for a in self._n], get=self._get("node", lambda: self._n))
        self.services = SimpleNamespace(list=lambda: [Thing(self, a, "service") for a in self._s], create=self._create,
                                        get=self._get("service", lambda: self._s))
        self.api = SimpleNamespace(tasks=lambda **kw: self._t)
        self.swarm = SimpleNamespace(init=self._init, leave=self._leave, reload=lambda: None,
                                     attrs={"JoinTokens": {"Worker": "SWMTKN-1-worker-secret", "Manager": "SWMTKN-1-manager-secret"}})

    def _get(self, kind, items):
        def get(i):
            self._maybe()
            return Thing(self, next(a for a in items() if a["ID"] == i), kind)
        return get

    def _maybe(self):
        if self.refuse:
            raise self.refuse

    def info(self):
        return {"Swarm": {"LocalNodeState": self.local, "ControlAvailable": self.control, "NodeID": SAM_ID,
                          "NodeAddr": "192.168.1.10", "Error": ""}}

    def _create(self, image, **kw):
        self._maybe()
        self.calls.append(("service.create", image, kw))

    def _init(self, **kw):
        self._maybe()
        self.calls.append(("swarm.init", kw))

    def _leave(self, force=False):
        self._maybe()
        self.calls.append(("swarm.leave", force))


@pytest.fixture
def docker(monkeypatch):
    d = Docker()
    monkeypatch.setattr(containers, "dclient", lambda: d)
    return d


def send(client, path, **body):
    return client.post(path, json={"password": PASSWORD, **body})


def test_a_swarm_is_read_into_what_the_page_shows(client, docker):
    _agents()
    c = client.get("/api/clusters").json()
    sw = next(x for x in c["clusters"] if x["kind"] == "swarm")
    assert sw["name"] == "Docker Swarm" and sw["status"] == "attention"
    assert sw["note"] == ("pi5 is down. The cluster still works: 2 of 3 managers agree. "
                          "One more failure would stop it making changes.")
    assert c["swarm"]["state"] == "manager" and c["tiles"]["services"] == {"services": 4, "full": 3}
    assert c["tiles"]["attention"] == 1 and c["tiles"]["machines"] == 3 and c["tiles"]["online"] == 2

    d = client.get("/api/clusters/swarm").json()
    assert d["address"] == "192.168.1.10:2377"
    n = {x["name"]: x for x in d["nodes"]}
    assert [x["name"] for x in d["nodes"]] == ["nas", "pi5", "sam-server"]
    assert n["sam-server"]["leader"] and n["sam-server"]["this"] and n["sam-server"]["tasks"] == 3
    assert n["pi5"]["state"] == "down" and n["pi5"]["since"] and n["pi5"]["tasks"] == 0 and n["pi5"]["reachable"] is False
    assert n["nas"]["tasks"] == 3 and n["nas"]["cpu"] == 4 and n["nas"]["memory"] == 22   # matched to the agent by name
    assert d["quorum"]["managers"] == 3 and d["quorum"]["up"] == 2 and d["quorum"]["needed"] == 2 and d["quorum"]["state"] == "none"

    s = {x["name"]: x for x in d["services"]}
    assert s["caddy"]["image"] == "caddy:2-alpine" and s["caddy"]["mode"] == "global"
    assert (s["caddy"]["running"], s["caddy"]["wanted"], s["caddy"]["state"]) == (2, 3, "warn")
    assert s["caddy"]["rule"] == "One on every machine" and s["caddy"]["where"] == ["nas", "sam-server"]
    assert s["pihole"]["rule"] == "1 copy, pinned to nas" and s["pihole"]["pinned"] == "nas" and s["pihole"]["state"] == "ok"
    assert s["pihole"]["ports"] == [{"published": 8053, "target": 80, "protocol": "tcp"}]
    assert s["homepage"]["rule"] == "2 copies, any machine" and (s["homepage"]["running"], s["homepage"]["wanted"]) == (2, 2)
    assert [e["service"] for e in d["events"]] == ["homepage"] and d["events"][0]["message"] == "task: non-zero exit (1)"
    assert "_raw" not in d and "tasks" not in d


def test_a_swarm_that_is_fine_says_nothing(client, monkeypatch):
    ok = [node(SAM_ID, "sam-server", leader=True), node(NAS_ID, "nas"), node(PI_ID, "pi5")]
    monkeypatch.setattr(containers, "dclient", lambda: Docker(nodes=ok, tasks=[t for t in TASKS if t["Status"]["State"] == "running"]
                                                              + [task(CADDY, PI_ID)]))
    sw = next(x for x in client.get("/api/clusters").json()["clusters"] if x["kind"] == "swarm")
    assert sw["status"] == "healthy" and sw["note"] is None
    assert client.get("/api/clusters/swarm").json()["quorum"] == {
        "managers": 3, "up": 3, "needed": 2, "spare": 1, "state": "ok", "text": "3 of 3 managers are up. It needs 2.",
        "detail": "It can lose 1 more manager and carry on."}


@pytest.mark.parametrize("managers,up,state", [(1, 1, "single"), (2, 2, "none"), (3, 3, "ok"), (3, 2, "none"),
                                                (3, 1, "lost"), (5, 3, "none"), (5, 4, "ok"), (2, 1, "lost")])
def test_quorum(managers, up, state):
    nodes = [{"role": "manager", "state": "ready" if i < up else "down", "reachable": i < up} for i in range(managers)]
    q = clusters._quorum(nodes)
    assert q["state"] == state and q["needed"] == managers // 2 + 1 and q["up"] == up


def test_lost_quorum_is_a_down_cluster(client, monkeypatch):
    nodes = [node(SAM_ID, "sam-server", leader=True), node(NAS_ID, "nas", state="down", reach="unreachable"),
             node(PI_ID, "pi5", state="down", reach="unreachable")]
    monkeypatch.setattr(containers, "dclient", lambda: Docker(nodes=nodes))
    sw = next(x for x in client.get("/api/clusters").json()["clusters"] if x["kind"] == "swarm")
    assert sw["status"] == "down" and "nas and pi5 are down." in sw["note"] and "It needs 2." in sw["note"]
    assert "can't accept changes" in sw["note"]


def test_no_swarm_no_card(client, monkeypatch):
    monkeypatch.setattr(containers, "dclient", lambda: Docker(local="inactive"))
    c = client.get("/api/clusters").json()
    assert c["swarm"]["state"] == "off" and not [x for x in c["clusters"] if x["kind"] == "swarm"]
    assert client.get("/api/clusters/swarm").status_code == 404


def test_a_worker_can_only_say_so(client, monkeypatch):
    monkeypatch.setattr(containers, "dclient", lambda: Docker(control=False))
    c = client.get("/api/clusters").json()
    sw = next(x for x in c["clusters"] if x["kind"] == "swarm")
    assert sw["status"] == "limited" and "worker" in sw["note"] and c["tiles"]["attention"] == 0
    assert client.get("/api/clusters/swarm").status_code == 404


def test_docker_out_of_reach_is_said_not_crashed(client, monkeypatch):
    def broken():
        raise RuntimeError("no socket")
    monkeypatch.setattr(containers, "dclient", broken)
    c = client.get("/api/clusters")
    assert c.status_code == 200
    assert not [x for x in c.json()["clusters"] if x["kind"] == "swarm"]      # no Docker, so no Swarm card to worry about
    assert c.json()["swarm"]["state"] == "unavailable" and "no socket" in c.json()["swarm"]["message"]
    r = send(client, "/api/clusters/swarm/init", name="Lab")
    assert r.status_code == 409


@pytest.mark.parametrize("local,word", [("locked", "unlock"), ("pending", "starting"), ("error", "problem")])
def test_a_swarm_in_trouble_is_explained(client, monkeypatch, local, word):
    monkeypatch.setattr(containers, "dclient", lambda: Docker(local=local))
    sw = next(x for x in client.get("/api/clusters").json()["clusters"] if x["kind"] == "swarm")
    assert word in sw["note"] and sw["status"] == "attention"


def test_docker_is_asked_once_for_a_burst_of_pages(client, docker, monkeypatch):
    asked = []
    orig = docker.info
    monkeypatch.setattr(docker, "info", lambda: asked.append(1) or orig())
    for _ in range(4):
        client.get("/api/clusters")
    assert len(asked) == 1
    client.get("/api/clusters?fresh=true")
    assert len(asked) == 2


def test_a_swarms_name_is_its_own(client, docker):
    assert send(client, "/api/clusters/swarm/rename", name="Lab Swarm").status_code == 200
    assert next(x for x in client.get("/api/clusters").json()["clusters"] if x["kind"] == "swarm")["name"] == "Lab Swarm"
    client.post("/api/clusters/groups", json={"name": "Other", "servers": []})
    assert send(client, "/api/clusters/swarm/rename", name="other").status_code == 409
    assert client.post("/api/clusters/groups", json={"name": "lab swarm", "servers": []}).status_code == 409
    assert send(client, "/api/clusters/swarm/rename", name="<x>").status_code == 400


# --------------------------------------------------------------------------- Swarm: changing it
CHANGES = [("/api/clusters/swarm/init", {"name": "Lab"}),
           ("/api/clusters/swarm/token", {}),
           (f"/api/clusters/swarm/nodes/{NAS_ID}/availability", {"availability": "drain"}),
           (f"/api/clusters/swarm/nodes/{PI_ID}/remove", {}),
           ("/api/clusters/swarm/services", {"name": "web", "image": "nginx:alpine"}),
           (f"/api/clusters/swarm/services/{HOME}/scale", {"copies": 3}),
           (f"/api/clusters/swarm/services/{HOME}/restart", {}),
           (f"/api/clusters/swarm/services/{HOME}/remove", {}),
           ("/api/clusters/swarm/leave", {})]


@pytest.mark.parametrize("path,body", CHANGES)
def test_changes_are_off_until_turned_on(client, monkeypatch, path, body):
    monkeypatch.setattr(clusters, "ALLOWED", False)
    d = Docker(local="inactive") if path.endswith("/init") else Docker()
    monkeypatch.setattr(containers, "dclient", lambda: d)
    r = send(client, path, **body)
    assert r.status_code == 409 and "LABKEEL_ALLOW_CLUSTER_CHANGES=1" in r.json()["error"], r.text
    assert not d.calls
    assert client.get("/api/clusters").json()["changes"] is False


@pytest.mark.parametrize("path,body", CHANGES)
def test_changes_ask_for_the_password(client, monkeypatch, path, body):
    d = Docker(local="inactive") if path.endswith("/init") else Docker()
    monkeypatch.setattr(containers, "dclient", lambda: d)
    assert client.post(path, json=body).status_code == 428
    r = client.post(path, json={**body, "password": "wrong"})
    assert r.status_code == 403 and r.headers.get("x-password") == "wrong"
    assert not d.calls


def test_starting_a_swarm(client, monkeypatch):
    d = Docker(local="inactive")
    monkeypatch.setattr(containers, "dclient", lambda: d)
    assert send(client, "/api/clusters/swarm/init", name="Lab Swarm", address="192.168.1.10").status_code == 200
    assert d.calls == [("swarm.init", {"advertise_addr": "192.168.1.10", "listen_addr": "0.0.0.0:2377"})]
    assert clusters.swarm_name() == "Lab Swarm"
    assert "Lab Swarm" in logged(client, "Started a Docker Swarm")["text"]
    d.local = "active"
    clusters._changed()
    assert send(client, "/api/clusters/swarm/init", name="Again").status_code == 409     # one at a time


@pytest.mark.parametrize("addr", ["not an address", "a b", "-x", "1.2.3.4:99999999", "x" * 300, "http://x", "a;b"])
def test_a_swarm_needs_a_sensible_address(client, monkeypatch, addr):
    d = Docker(local="inactive")
    monkeypatch.setattr(containers, "dclient", lambda: d)
    assert send(client, "/api/clusters/swarm/init", name="Lab", address=addr).status_code == 400
    assert not d.calls


def test_docker_refusing_to_start_a_swarm_is_explained(client, monkeypatch):
    import docker as sdk
    d = Docker(local="inactive")
    d.refuse = sdk.errors.APIError("x", explanation="could not choose an IP address to advertise since this system has multiple addresses")
    monkeypatch.setattr(containers, "dclient", lambda: d)
    r = send(client, "/api/clusters/swarm/init", name="Lab")
    assert r.status_code == 409 and "multiple addresses" in r.json()["error"] and "Type this machine's own address" in r.json()["error"]


def test_the_join_command_is_given_once_and_never_logged(client, docker):
    r = send(client, "/api/clusters/swarm/token", role="worker")
    assert r.status_code == 200
    assert r.json()["command"] == "docker swarm join --token SWMTKN-1-worker-secret 192.168.1.10:2377"
    assert send(client, "/api/clusters/swarm/token", role="manager").json()["token"] == "SWMTKN-1-manager-secret"
    assert send(client, "/api/clusters/swarm/token", role="owner").status_code == 400
    for path in ("/api/clusters", "/api/clusters/swarm"):
        assert "SWMTKN" not in client.get(path).text
    assert "SWMTKN" not in str(client.get("/api/events", params={"q": "join"}).json())
    assert logged(client, "Showed the Swarm join command")


def test_what_draining_would_do_is_shown_first(client, docker):
    p = client.get(f"/api/clusters/swarm/nodes/{SAM_ID}/preview").json()
    assert p["node"] == "sam-server" and p["availability"] == "active"
    rows = {r["service"]: r for r in p["rows"]}
    assert rows["caddy"]["outcome"] == "stops" and rows["caddy"]["text"] == "Stops (one per machine)"
    assert rows["vaultwarden"]["outcome"] == "stuck" and rows["vaultwarden"]["text"] == "Can't move: pinned here"
    assert rows["homepage"] == {"service": "homepage", "copies": 1, "outcome": "moves", "text": "To nas"}
    assert p["stops"] == ["vaultwarden"] and "pihole" not in rows
    assert not docker.calls
    assert client.get("/api/clusters/swarm/nodes/zzzzzzzzzzzz/preview").status_code == 404
    assert client.get("/api/clusters/swarm/nodes/x/preview").status_code == 404


def test_draining_with_nowhere_to_go(client, monkeypatch):
    only = [node(SAM_ID, "sam-server", leader=True)]
    monkeypatch.setattr(containers, "dclient", lambda: Docker(nodes=only, tasks=[task(HOME, SAM_ID)]))
    rows = client.get(f"/api/clusters/swarm/nodes/{SAM_ID}/preview").json()["rows"]
    assert rows[0]["outcome"] == "stuck" and "no other machine" in rows[0]["text"]


def test_drain_and_make_active_again(client, docker):
    assert send(client, f"/api/clusters/swarm/nodes/{NAS_ID}/availability", availability="drain").status_code == 200
    call = docker.calls[-1]
    assert call[0] == "node.update" and call[1] == NAS_ID and call[2]["Availability"] == "drain" and call[2]["Role"] == "manager"
    assert "Drained nas" in logged(client, "Drained nas")["text"]
    assert send(client, f"/api/clusters/swarm/nodes/{NAS_ID}/availability", availability="active").status_code == 200
    assert docker.calls[-1][2]["Availability"] == "active" and logged(client, "Made active again")
    assert send(client, f"/api/clusters/swarm/nodes/{NAS_ID}/availability", availability="pause").status_code == 400
    assert send(client, "/api/clusters/swarm/nodes/zzzzzzzzzzzz/availability", availability="drain").status_code == 404


def test_only_a_machine_that_is_down_can_be_removed(client, docker):
    r = send(client, f"/api/clusters/swarm/nodes/{NAS_ID}/remove")
    assert r.status_code == 409 and "still ready" in r.json()["error"]
    assert send(client, f"/api/clusters/swarm/nodes/{PI_ID}/remove").status_code == 200
    assert docker.calls == [("node.remove", PI_ID, False)] and logged(client, "Removed pi5 from the Swarm")


def test_scaling_a_service(client, docker):
    assert send(client, f"/api/clusters/swarm/services/{HOME}/scale", copies=3).status_code == 200
    assert docker.calls == [("service.scale", HOME, 3)] and "from 2 to 3" in logged(client, "Scaled the Swarm service homepage")["text"]
    for bad in (-1, 51, "x", None, 2.5e30, True, [1], {}):
        r = send(client, f"/api/clusters/swarm/services/{HOME}/scale", copies=bad)
        assert r.status_code == 400, bad
    assert send(client, f"/api/clusters/swarm/services/{HOME}/scale", copies=0).status_code == 200      # stopping it is fine
    r = send(client, f"/api/clusters/swarm/services/{CADDY}/scale", copies=2)
    assert r.status_code == 409 and "one on every machine" in r.json()["error"]
    assert send(client, "/api/clusters/swarm/services/zzzzzzzzzzzz/scale", copies=2).status_code == 404


def test_restarting_and_removing_a_service(client, docker):
    assert send(client, f"/api/clusters/swarm/services/{PIHOLE}/restart").status_code == 200
    assert send(client, f"/api/clusters/swarm/services/{PIHOLE}/remove").status_code == 200
    assert docker.calls == [("service.force_update", PIHOLE), ("service.remove", PIHOLE, False)]
    assert logged(client, "Restarted the Swarm service pihole") and logged(client, "Removed the Swarm service pihole")


def test_adding_a_service(client, docker):
    r = send(client, "/api/clusters/swarm/services", name="web", image="nginx:alpine", copies=2, published=8080, target=80)
    assert r.status_code == 200, r.text
    kind, image, kw = docker.calls[-1]
    assert (kind, image, kw["name"]) == ("service.create", "nginx:alpine", "web")
    assert kw["mode"]["replicated"]["Replicas"] == 2
    assert kw["endpoint_spec"]["Ports"] == [{"Protocol": "tcp", "PublishedPort": 8080, "TargetPort": 80}]
    assert "Added the Swarm service web from nginx:alpine, 2 copies" in logged(client, "Added the Swarm service web")["text"]
    send(client, "/api/clusters/swarm/services", name="agent", image="ghcr.io/x/agent:1", mode="global")
    kw = docker.calls[-1][2]
    assert "global" in kw["mode"] and "endpoint_spec" not in kw
    send(client, "/api/clusters/swarm/services", name="one", image="redis:7", published=6379)       # one number: the same both sides
    assert docker.calls[-1][2]["endpoint_spec"]["Ports"][0]["TargetPort"] == 6379


@pytest.mark.parametrize("body,status", [({"name": "", "image": "x"}, 400), ({"name": "-x", "image": "x"}, 400),
                                         ({"name": "web", "image": "--privileged"}, 400),
                                         ({"name": "web", "image": "a b"}, 400), ({"name": "web", "image": ""}, 400),
                                         ({"name": "web", "image": "x", "copies": 99}, 400),
                                         ({"name": "web", "image": "x", "mode": "both"}, 400),
                                         ({"name": "web", "image": "x", "published": 0}, 400),
                                         ({"name": "web", "image": "x", "published": 70000, "target": 80}, 400),
                                         ({"name": "caddy", "image": "x"}, 409), ({"name": "CADDY", "image": "x"}, 409)])
def test_a_service_has_to_make_sense(client, docker, body, status):
    assert send(client, "/api/clusters/swarm/services", **body).status_code == status
    assert not docker.calls


def test_docker_saying_no_is_passed_on(client, docker):
    import docker as sdk
    docker.refuse = sdk.errors.APIError("x", explanation="rpc error: node is not down")
    r = send(client, f"/api/clusters/swarm/services/{HOME}/scale", copies=3)
    assert r.status_code == 409 and "Docker wouldn't change that service" in r.json()["error"] and "node is not down" in r.json()["error"]


def test_leaving_the_swarm(client, docker):
    clusters.U()._write("clusters.json", {"groups": {}, "swarm_name": "Lab"})
    assert send(client, "/api/clusters/swarm/leave").status_code == 200
    assert docker.calls == [("swarm.leave", True)] and "swarm_name" not in clusters._load()
    assert logged(client, "Left the Docker Swarm")


def test_changes_need_a_swarm_to_change(client, monkeypatch):
    d = Docker(local="inactive")
    monkeypatch.setattr(containers, "dclient", lambda: d)
    assert send(client, f"/api/clusters/swarm/services/{HOME}/restart").status_code == 409
    assert send(client, "/api/clusters/swarm/token").status_code == 409
    assert not d.calls


def test_the_password_is_asked_for_once_in_a_while(client, docker):
    r = client.post(f"/api/clusters/swarm/services/{HOME}/restart", json={})
    assert r.status_code == 428
    assert send(client, f"/api/clusters/swarm/services/{HOME}/restart").status_code == 200
    assert client.post(f"/api/clusters/swarm/services/{PIHOLE}/restart", json={}).status_code == 200   # the same session, soon after


def test_timestamps():
    assert clusters._epoch("2026-11-13T10:00:00.123456789Z") == 1794564000.0
    assert clusters._epoch("2026-11-13T11:00:00+01:00") == 1794564000.0
    assert clusters._epoch("0001-01-01T00:00:00Z") is None
    assert clusters._epoch("yesterday") is None and clusters._epoch(None) is None and clusters._epoch(5) is None

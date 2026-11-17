"""The Overview's history: network traffic is recorded and reported, and the Power card's line comes from the server."""
import time

import main
import metrics


def _clear():
    with metrics._db_lock:
        c = metrics.db()
        c.execute("DELETE FROM m1")
        c.execute("DELETE FROM m10")
        c.commit()


def test_network_traffic_is_kept_and_totalled(client):
    _clear()
    now = int(time.time()) // 60 * 60
    rows = []
    for i in range(1, 11):                                  # ten minutes: 1 MB/s down, 0.25 MB/s up
        rows += [("n_rx_bps", now - i * 60, 1_000_000.0), ("n_tx_bps", now - i * 60, 250_000.0)]
    rows.append(("net_rx", now - 60, 9e12))                 # an older version's row: not ours, never shown
    with metrics._db_lock:
        metrics.db().executemany("INSERT OR REPLACE INTO m1 (k, t, v) VALUES (?, ?, ?)", rows)
        metrics.db().commit()
    d = client.get("/api/history?range=hour").json()
    assert len(d["series"]["n_rx_bps"]) == 10 and "net_rx" not in d["series"]
    assert d["net"] == {"rx_bytes": 600_000_000, "tx_bytes": 150_000_000}
    _clear()


def test_the_power_line_survives_a_reload(client):
    with main._sys_lock:
        main._sys["history"].append({"t": time.time(), "cpu": 1, "mem": 2, "rx": 3, "tx": 4, "gpu": [], "pw": 87.5})
    h = client.get("/api/system?since=0").json()["history"]
    assert h[-1]["pw"] == 87.5

"""Overview → Network → a card: one network card in detail."""
import socket
from collections import namedtuple

import psutil
import pytest

import nics

Stat = namedtuple("Stat", "isup duplex speed mtu flags")
Addr = namedtuple("Addr", "family address netmask broadcast ptp")
IO = namedtuple("IO", "bytes_sent bytes_recv packets_sent packets_recv errin errout dropin dropout")


@pytest.fixture
def card(monkeypatch, tmp_path):
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"wlp5s0": Stat(True, psutil.NIC_DUPLEX_UNKNOWN, 0, 1500, ""),
                                                          "eth0": Stat(True, psutil.NIC_DUPLEX_FULL, 2500, 9000, "")})
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"wlp5s0": [
        Addr(socket.AF_INET, "192.168.1.20", "255.255.255.0", None, None),
        Addr(socket.AF_INET6, "fe80::1%wlp5s0", "ffff:ffff:ffff:ffff::", None, None),
        Addr(socket.AF_INET6, "2a00:23c8::20", "ffff:ffff:ffff:ffff::", None, None),
        Addr(psutil.AF_LINK, "aa:bb:cc:dd:ee:ff", None, None, None)]})
    monkeypatch.setattr(psutil, "net_io_counters", lambda pernic=False: {"wlp5s0": IO(2000, 5000, 20, 50, 3, 0, 1, 0)})
    sysnet = tmp_path / "net"
    (sysnet / "wlp5s0" / "wireless").mkdir(parents=True)
    (sysnet / "wlp5s0" / "device").mkdir()
    (sysnet / "wlp5s0" / "operstate").write_text("up\n")
    monkeypatch.setattr(nics, "SYS", str(sysnet))
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\tFlags\nwlp5s0\t00000000\t0101A8C0\t0003\nwlp5s0\t0001A8C0\t00000000\t0001\n")
    wireless = tmp_path / "wireless"
    wireless.write_text("Inter-| sta-|   Quality        |\n face | tus | link level noise |\nwlp5s0: 0000   56.  -54.  -256\n")
    real = nics._read
    monkeypatch.setattr(nics, "_read", lambda p: real({"/proc/net/route": str(route), "/proc/net/wireless": str(wireless)}.get(p, p)))
    nics._iw.clear()
    return sysnet


def test_a_wifi_card_in_detail(client, card, monkeypatch):
    import main
    monkeypatch.setattr(main, "HOST_NS", True)
    monkeypatch.setattr(main, "host_cmd", lambda args, timeout=15, input=None: type("R", (), {
        "returncode": 0, "stdout": "Connected to aa:bb\n\tSSID: Home Wi-Fi\n\tfreq: 5180.0\n\tsignal: -54 dBm\n\ttx bitrate: 866.7 MBit/s VHT-MCS 9\n"})())
    d = client.get("/api/network/nic/wlp5s0").json()
    assert (d["kind"], d["up"], d["mtu"], d["mac"], d["gateway"], d["operstate"]) == ("Wi-Fi", True, 1500, "aa:bb:cc:dd:ee:ff", "192.168.1.1", "up")
    assert d["ipv4"] == [{"address": "192.168.1.20", "prefix": 24}]
    assert [a["local"] for a in d["ipv6"]] == [True, False] and d["ipv6"][0]["address"] == "fe80::1"
    assert d["wifi"] == {"quality": 80, "signal_dbm": -54, "ssid": "Home Wi-Fi", "freq_mhz": 5180, "tx_bitrate": "866.7 MBit/s"}
    assert d["counters"]["rx_errors"] == 3 and d["counters"]["rx_dropped"] == 1


def test_without_iw_the_signal_still_shows(client, card, monkeypatch):
    import main
    monkeypatch.setattr(main, "HOST_NS", False)
    assert client.get("/api/network/nic/wlp5s0").json()["wifi"] == {"quality": 80, "signal_dbm": -54}


def test_an_ethernet_card_with_no_counters(client, card):
    (card / "eth0" / "device").mkdir(parents=True)
    d = client.get("/api/network/nic/eth0").json()
    assert (d["kind"], d["speed_mbps"], d["duplex"], d["counters"], d["wifi"], d["gateway"]) == ("Ethernet", 2500, "full", None, None, None)


@pytest.mark.parametrize("name", ["nope0", "..", "a" * 40, "eth0;ls"])
def test_only_a_card_that_exists(client, card, name):
    assert client.get(f"/api/network/nic/{name}").status_code == 404


def test_viewers_may_look(card):
    import security
    assert security.viewer_allowed("GET", "/api/network/nic/wlp5s0")
    assert not security.viewer_allowed("POST", "/api/network/nic/wlp5s0")


def test_the_panel_lists_cards_as_buttons():
    from pathlib import Path
    js = (Path(nics.__file__).parent / "static" / "ui" / "overview.js").read_text()
    assert 'class="nic" type="button" data-nic=' in js and "NicView.open(b.dataset.nic)" in js and "‹ Network traffic" in js

"""One network card in detail, for Overview → Network → a card: what it is, its addresses, its link (speed, or
Wi-Fi signal), and its counters (packets, errors, dropped). Read-only."""
import os
import re
import socket
import sys
import time

import psutil
from fastapi import APIRouter, HTTPException

router = APIRouter()
SYS = "/sys/class/net"
NAME = re.compile(r"[A-Za-z0-9_.:@-]{1,32}")
_iw = {}            # name -> (time, details): asking iw is slower, so once every 15 s is plenty


def M():
    return sys.modules["main"]


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def kind(name):
    """What sort of card this is, in words."""
    if name.startswith("tailscale"):
        return "Tailscale"
    if os.path.exists(f"{SYS}/{name}/wireless") or os.path.exists(f"{SYS}/{name}/phy80211"):
        return "Wi-Fi"
    if os.path.exists(f"{SYS}/{name}/device"):
        return "Ethernet"
    if name.startswith(("wg", "tun", "tap")):
        return "VPN tunnel"
    if os.path.exists(f"{SYS}/{name}/bridge"):
        return "Bridge"
    return "Virtual"


def _prefix(mask):
    try:
        if ":" in mask:
            return sum(bin(int(p, 16)).count("1") for p in mask.split(":") if p)
        return sum(bin(int(p)).count("1") for p in mask.split("."))
    except (ValueError, AttributeError):
        return None


def _gateway(name):
    """The IPv4 router this card sends to by default, from the kernel's route table (None: not the way out)."""
    txt = _read("/proc/net/route") or ""
    for ln in txt.splitlines()[1:]:
        f = ln.split()
        if len(f) >= 3 and f[0] == name and f[1] == "00000000":
            try:
                return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
            except (ValueError, OverflowError):
                return None
    return None


def _wireless(name):
    """Signal and link quality from /proc/net/wireless, and (when it can ask the machine) the network's name,
    band and speeds from iw. Any of it may be missing."""
    out = {}
    for ln in (_read("/proc/net/wireless") or "").splitlines()[2:]:
        f = ln.replace(":", " ", 1).split()
        if len(f) >= 4 and f[0] == name:
            try:
                out["quality"] = round(float(f[2].rstrip(".")) / 70 * 100)
                lvl = float(f[3].rstrip("."))
                out["signal_dbm"] = round(lvl - 256 if lvl > 0 else lvl)
            except ValueError:
                pass
    hit = _iw.get(name)
    if hit and time.time() - hit[0] < 15:
        return {**out, **hit[1]}
    got = {}
    try:
        r = M().host_cmd(["iw", "dev", name, "link"], timeout=5) if M().HOST_NS else None
        txt = r.stdout if r is not None and r.returncode == 0 else ""
        for key, pat in (("ssid", r"SSID:\s*(.+)"), ("freq_mhz", r"freq:\s*([\d.]+)"), ("signal_dbm", r"signal:\s*(-?\d+)"),
                         ("tx_bitrate", r"tx bitrate:\s*([\d.]+ \S+)"), ("rx_bitrate", r"rx bitrate:\s*([\d.]+ \S+)")):
            m = re.search(pat, txt)
            if m:
                v = m.group(1).strip()
                got[key] = round(float(v)) if key == "freq_mhz" else int(v) if key == "signal_dbm" else v[:60]
    except Exception:
        pass
    _iw[name] = (time.time(), got)
    return {**out, **got}


@router.get("/api/network/nic/{name}")
def api_nic(name: str):
    stats = psutil.net_if_stats()
    if not NAME.fullmatch(name) or name not in stats:
        raise HTTPException(404, "There's no network card by that name.")
    st, io = stats[name], psutil.net_io_counters(pernic=True).get(name)
    addrs = psutil.net_if_addrs().get(name, [])
    v4 = [{"address": a.address, "prefix": _prefix(a.netmask)} for a in addrs if a.family == socket.AF_INET]
    v6 = [{"address": a.address.split("%")[0], "prefix": _prefix(a.netmask), "local": a.address.lower().startswith("fe80")}
          for a in addrs if a.family == socket.AF_INET6]
    mac = next((a.address for a in addrs if a.family == psutil.AF_LINK and a.address and a.address != "00:00:00:00:00:00"), None)
    drv = os.path.realpath(f"{SYS}/{name}/device/driver") if os.path.exists(f"{SYS}/{name}/device/driver") else None
    live = next((n for n in (M()._sys.get("current") or {}).get("network", {}).get("interfaces", []) if n["name"] == name), {})
    k = kind(name)
    duplex = {psutil.NIC_DUPLEX_FULL: "full", psutil.NIC_DUPLEX_HALF: "half"}.get(st.duplex)
    return {
        "name": name, "kind": k, "up": st.isup, "speed_mbps": st.speed or None, "duplex": duplex, "mtu": st.mtu,
        "mac": mac, "ipv4": v4, "ipv6": v6, "gateway": _gateway(name), "driver": os.path.basename(drv) if drv else None,
        "operstate": _read(f"{SYS}/{name}/operstate"),
        "rx_rate": live.get("rx_rate"), "tx_rate": live.get("tx_rate"),
        "counters": {"rx_bytes": io.bytes_recv, "tx_bytes": io.bytes_sent, "rx_packets": io.packets_recv,
                     "tx_packets": io.packets_sent, "rx_errors": io.errin, "tx_errors": io.errout,
                     "rx_dropped": io.dropin, "tx_dropped": io.dropout} if io else None,
        "wifi": _wireless(name) if k == "Wi-Fi" else None,
        "since_boot": psutil.boot_time(),
    }

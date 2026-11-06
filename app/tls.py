"""
HTTPS certificates for the dashboard.

LABKEEL_HTTPS=auto (default) picks the first that works:
  1. your own certificate in /certs (tls.crt + tls.key, or fullchain.pem + privkey.pem)
  2. a trusted Tailscale certificate for this machine's *.ts.net name (needs HTTPS turned on in
     the Tailscale admin console). Fetched with the host's `tailscale cert` and renewed automatically.
  3. a self-signed certificate made here: still encrypted, but browsers show a warning
LABKEEL_HTTPS=tailscale | self-signed | off forces one of them ("off" = plain HTTP, e.g. behind Caddy).
"""
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import env

MODE = env.text("LABKEEL_HTTPS", "auto").strip().lower() or "auto"
USER_CERTS = Path(env.text("LABKEEL_CERTS_DIR", "/certs"))
state = {"mode": "off" if MODE == "off" else None, "domain": None, "expires": None, "issuer": None,
         "error": None, "cert": None, "key": None, "fingerprint": None}
_ctx_getter = None


def M():
    """The main module, looked up when needed."""
    return sys.modules["main"]


def tls_dir():
    """data/tls, where certificates fetched or made here are kept."""
    d = M().DATA_DIR / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_private(path, data):
    """Write a key or certificate readable only by its owner, swapped in whole."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    tmp.replace(path)


def info(cert_path):
    """A certificate's expiry, issuer, fingerprint and names."""
    c = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    try:
        issuer = c.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value
    except IndexError:
        issuer = c.issuer.rfc4514_string()
    try:
        names = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    return {"expires": c.not_valid_after_utc.timestamp(), "issuer": issuer,
            "fingerprint": c.fingerprint(hashes.SHA256()).hex(), "names": names}


# --------------------------------------------------------------------------- sources
def _user_certs():
    """Your own certificate and key in /certs, if both are there (several common file names work)."""
    for crt, key in (("tls.crt", "tls.key"), ("fullchain.pem", "privkey.pem"), ("cert.pem", "key.pem")):
        if (USER_CERTS / crt).is_file() and (USER_CERTS / key).is_file():
            return USER_CERTS / crt, USER_CERTS / key
    return None


class NotAvailable(Exception):
    """Tailscale can't give a certificate here (not installed, not signed in, or HTTPS off for the tailnet)."""


def _tailscale():
    """A trusted certificate for this machine's *.ts.net name, from the host's `tailscale cert`."""
    host = M().host_cmd
    try:
        r = host(["tailscale", "status", "--json"], timeout=15)
    except FileNotFoundError:
        raise NotAvailable("Tailscale isn't installed on this machine")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise NotAvailable("Tailscale isn't available here" + (f": {msg[-1]}" if msg else ""))
    try:
        st = json.loads(r.stdout)
    except ValueError:
        raise NotAvailable("Couldn't read Tailscale's status")
    domains = [d.rstrip(".") for d in (st.get("CertDomains") or []) if d]
    if not domains:
        raise NotAvailable("HTTPS certificates are turned off for your tailnet. Turn on HTTPS on the DNS page of the Tailscale admin console.")
    domain = domains[0]
    tmp = host(["mktemp", "-d"], timeout=10).stdout.strip()
    if not tmp.startswith("/"):
        raise RuntimeError("couldn't make a temporary folder on the host")
    try:
        r = host(["tailscale", "cert", "--cert-file", f"{tmp}/c.pem", "--key-file", f"{tmp}/k.pem", domain], timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"tailscale cert failed: {(r.stderr or r.stdout).strip()[:300]}")
        crt = host(["cat", f"{tmp}/c.pem"], timeout=10).stdout
        key = host(["cat", f"{tmp}/k.pem"], timeout=10).stdout
    finally:
        host(["rm", "-rf", tmp], timeout=10)
    if "BEGIN CERTIFICATE" not in crt or "PRIVATE KEY" not in key:
        raise RuntimeError("tailscale cert returned something unexpected")
    d = tls_dir()
    _write_private(d / "tailscale.key", key)
    _write_private(d / "tailscale.crt", crt)
    return d / "tailscale.crt", d / "tailscale.key", domain


def _local_names():
    """This machine's names and IPv4 addresses, for a self-signed certificate."""
    dns, ips = {"localhost", socket.gethostname()}, {"127.0.0.1"}
    try:
        dns.add(Path("/proc/sys/kernel/hostname").read_text().strip())
    except OSError:
        pass
    for nic, addrs in psutil.net_if_addrs().items():
        if nic.startswith(("veth", "docker", "br-")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET:
                ips.add(a.address)
    return sorted(n for n in dns if n), sorted(ips)


def _self_signed():
    """A self-signed certificate for this machine's names and addresses: the one made earlier while it's valid
    for another 30 days and covers them all, otherwise a new one."""
    d = tls_dir()
    crt, key = d / "self-signed.crt", d / "self-signed.key"
    dns, ips = _local_names()
    if crt.is_file() and key.is_file():
        try:
            c = x509.load_pem_x509_certificate(crt.read_bytes())
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have = set(san.get_values_for_type(x509.DNSName)) | {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            fresh = c.not_valid_after_utc - datetime.now(timezone.utc) > timedelta(days=30)
            if fresh and set(dns) | set(ips) <= have:
                return crt, key
        except Exception:
            pass
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns[0] if dns else "homelab"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LabKeel (self-signed)")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in dns] +
                                                       [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(k, hashes.SHA256()))
    _write_private(key, k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode())
    _write_private(crt, cert.public_bytes(serialization.Encoding.PEM).decode())
    return crt, key


# --------------------------------------------------------------------------- choose + reload
def obtain():
    """Pick a certificate. Returns (cert_path, key_path) and fills `state`."""
    err = None
    uc = _user_certs() if MODE in ("auto", "custom") else None
    if uc:
        crt, key = uc
        i = info(crt)
        names = i["names"] or [None]
        domain = next((n for n in names if n and not n.startswith("*.")), names[0])  # prefer a real name over *.example.com
        state.update(mode="custom", cert=str(crt), key=str(key), domain=domain, error=None, **_pick(i))
        return crt, key
    if MODE in ("auto", "tailscale"):
        try:
            crt, key, domain = _tailscale()
            state.update(mode="tailscale", cert=str(crt), key=str(key), domain=domain, error=None, **_pick(info(crt)))
            return crt, key
        except Exception as e:
            err = str(e)
            # a Tailscale hiccup shouldn't swap a trusted certificate for a self-signed one: keep the last
            # good one while it's still valid, and keep trying in the background
            prev = tls_dir() / "tailscale.crt"
            try:
                i = info(prev) if prev.is_file() and prev.with_suffix(".key").is_file() else None
            except Exception:
                i = None
            if i and i["expires"] > time.time() + 3600:
                state.update(mode="tailscale", cert=str(prev), key=str(prev.with_suffix(".key")),
                             domain=(i["names"] or [state.get("domain")])[0],
                             error=f"Couldn't renew the Tailscale certificate, still using the current one: {err}", **_pick(i))
                return prev, prev.with_suffix(".key")
            if MODE == "tailscale":
                raise
    crt, key = _self_signed()
    state.update(mode="self-signed", cert=str(crt), key=str(key), domain=None, error=err, **_pick(info(crt)))
    return crt, key


def _pick(i):
    """The certificate details that go into `state`."""
    return {"expires": i["expires"], "issuer": i["issuer"], "fingerprint": i["fingerprint"]}


def set_context_getter(fn):
    """serve.py hands over a way to reach the running server's TLS settings, so a renewed certificate can be
    swapped in without a restart."""
    global _ctx_getter
    _ctx_getter = fn


def refresh():
    """Re-check the certificate and swap it into the running server if it changed."""
    before = state.get("fingerprint")
    try:
        crt, key = obtain()
    except Exception as e:
        state["error"] = str(e)
        return False
    if state.get("fingerprint") == before or not _ctx_getter:
        return False
    ctx = _ctx_getter()
    if ctx is None:
        return False
    ctx.load_cert_chain(str(crt), str(key))
    print(f"[tls] Now using a {state['mode']} certificate (valid until "
          f"{datetime.fromtimestamp(state['expires']):%d %b %Y})", flush=True)
    return True


def renew_loop():
    """Background thread: re-checks the certificate twice a day (every 10 minutes while something's wrong)."""
    while True:
        # retry soon while something's wrong (on the fallback, or a renewal failed), otherwise twice a day
        time.sleep(600 if state.get("error") and MODE in ("auto", "tailscale") else 12 * 3600)
        try:
            refresh()
        except Exception as e:
            print("[tls] renew error:", e, flush=True)


def start_renewal():
    """Starts the renewal thread (unless HTTPS is off)."""
    if MODE != "off":
        threading.Thread(target=renew_loop, daemon=True, name="tls-renew").start()


def public_state():
    """The certificate in use, for Settings → Security."""
    return {k: state.get(k) for k in ("mode", "domain", "expires", "issuer", "error")}

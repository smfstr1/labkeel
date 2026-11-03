"""
Starts the dashboard.

With HTTPS on (the default), one port does both jobs: encrypted connections go through to the
app, and anyone who opens the old http:// address is redirected to the secure https:// one.
"""
import asyncio
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime

import uvicorn

import main
import security
import tls

HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8787"))
LOG_LEVEL = os.environ.get("DASH_LOG_LEVEL", "warning")
MAX_CONNECTIONS = int(os.environ.get("DASH_MAX_CONNECTIONS", "512"))
# With DASH_TLS=off behind a proxy such as Caddy: the proxy's address. Only then are its X-Forwarded-For
# (the real client, for the wrong-password limit) and X-Forwarded-Proto headers believed. Without it,
# anything on this machine could claim to be any address.
TRUSTED_PROXY = os.environ.get("DASH_TRUSTED_PROXY", "").strip()
MAX_PER_CLIENT = 64   # a browser opens about 6; this only stops one address hogging the server
HELLO_TIMEOUT = 10    # seconds a new connection gets to start talking (and to send a whole http:// request)
IDLE_TIMEOUT = 120    # an open connection with nothing moving in either direction is closed
_open = {"total": 0, "by_ip": {}}


def _close(writer):
    """Close a connection, ignoring errors (it may be closed already)."""
    try:
        writer.close()
    except Exception:
        pass


async def _relay(client_r, client_w, app_r, app_w):
    """Copy bytes both ways until either side closes, or nothing has moved for IDLE_TIMEOUT."""
    last = time.monotonic()

    async def pipe(reader, writer):
        nonlocal last
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                last = time.monotonic()
                writer.write(data)
                await writer.drain()  # a peer that stops reading stalls here, and the idle check below ends it
                last = time.monotonic()
        except (ConnectionError, OSError):
            pass
        finally:
            _close(writer)

    tasks = [asyncio.create_task(pipe(client_r, app_w)), asyncio.create_task(pipe(app_r, client_w))]
    try:
        while not all(t.done() for t in tasks):
            await asyncio.wait(tasks, timeout=min(15, IDLE_TIMEOUT))
            if time.monotonic() - last > IDLE_TIMEOUT:
                break
    finally:
        for t in tasks:
            t.cancel()
        _close(client_w)
        _close(app_w)
        await asyncio.gather(*tasks, return_exceptions=True)


def _redirect_target(host_header, path):
    """The https:// address to send an http:// visitor to: the certificate's own name where there is one (so the
    browser trusts it), otherwise the name they typed."""
    host = (host_header or "").strip()
    name = host.split("]")[0] + "]" if host.startswith("[") else re.sub(r":\d+$", "", host)
    if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,253}", name or ""):
        name = ""
    canonical = tls.state.get("domain") if tls.state.get("mode") in ("tailscale", "custom") else None
    if canonical and canonical.startswith("*."):  # wildcard-only certificate: keep the name they typed
        canonical = None
    target = canonical or name or "localhost"
    port = "" if PORT == 443 else f":{PORT}"
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    return f"https://{target}{port}{path}"


async def _plain_http_redirect(first, reader, writer):
    """Someone used http://: read their request (briefly) and redirect them to https://."""
    buf = bytearray(first)

    async def read_head():
        while b"\r\n\r\n" not in buf and len(buf) < 16384:
            more = await reader.read(4096)
            if not more:
                break
            buf.extend(more)

    try:  # one deadline for the whole request, so trickling a byte at a time doesn't keep it open
        await asyncio.wait_for(read_head(), timeout=HELLO_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    head = bytes(buf).split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace").split("\r\n")
    parts = head[0].split(" ") if head else []
    path = parts[1] if len(parts) >= 2 else "/"
    host = next((h.split(":", 1)[1].strip() for h in head[1:] if h.lower().startswith("host:")), "")
    loc = _redirect_target(host, path)
    body = f'<a href="{loc}">This dashboard only works over a secure connection. Continue to {loc}</a>'.encode()
    writer.write(b"HTTP/1.1 301 Moved Permanently\r\nLocation: " + loc.encode() +
                 b"\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: " + str(len(body)).encode() +
                 b"\r\nConnection: close\r\n\r\n" + body)
    try:
        await asyncio.wait_for(writer.drain(), timeout=HELLO_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        _close(writer)


def front_handler(internal_port):
    """The public port's connection handler. A new connection's first byte says whether it's HTTPS (passed
    through, still encrypted, to the app on an internal port) or plain HTTP (redirected). Limits connections
    per address and in total."""
    async def serve_one(reader, writer, peer):
        try:
            first = await asyncio.wait_for(reader.read(4096), timeout=HELLO_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            _close(writer)
            return
        if not first:
            _close(writer)
            return
        if first[0] != 0x16:  # not a TLS handshake: someone used http://
            await _plain_http_redirect(first, reader, writer)
            return
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", internal_port), timeout=HELLO_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            _close(writer)
            return
        local_port = uw.get_extra_info("sockname")[1]
        security.PEERS[local_port] = peer[0]  # so the app knows the real client address
        uw.write(first)
        try:
            await _relay(reader, writer, ur, uw)
        finally:
            security.PEERS.pop(local_port, None)

    async def handle(reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip, by_ip = peer[0], _open["by_ip"]
        if _open["total"] >= MAX_CONNECTIONS or by_ip.get(ip, 0) >= MAX_PER_CLIENT:
            transport = writer.transport
            if transport:
                transport.abort()
            return
        _open["total"] += 1
        by_ip[ip] = by_ip.get(ip, 0) + 1
        try:
            await serve_one(reader, writer, peer)
        finally:
            _open["total"] -= 1
            by_ip[ip] -= 1
            if by_ip[ip] <= 0:
                by_ip.pop(ip, None)
    return handle


def _warn_if_open():
    """A warning in the log when sign-in is off or not set up."""
    if security.enabled():
        return
    if security.open_ok():
        print("WARNING: sign-in is off (DASH_ALLOW_NO_AUTH=1). Anyone who can reach this port can use the dashboard. "
              "It only answers to this machine's own names and addresses (add others with DASH_HOSTNAMES).", flush=True)
    else:
        print("Sign-in isn't set up: the dashboard will only show a set-up page until DASH_USER and DASH_PASS are set.", flush=True)


async def run():
    """Start the dashboard: plain HTTP with DASH_TLS=off, otherwise HTTPS through the front port."""
    _warn_if_open()
    if tls.MODE == "off":
        print(f"Dashboard on http://{HOST}:{PORT} (HTTPS is off: DASH_TLS=off)", flush=True)
        config = uvicorn.Config(main.app, host=HOST, port=PORT, proxy_headers=bool(TRUSTED_PROXY),
                                forwarded_allow_ips=TRUSTED_PROXY or None, limit_concurrency=MAX_CONNECTIONS,
                                log_level=LOG_LEVEL, access_log=False, server_header=False)
        await uvicorn.Server(config).serve()
        return

    crt, key = await asyncio.to_thread(tls.obtain)
    st = tls.state
    where = st.get("domain") or (socket.gethostname() if HOST in ("0.0.0.0", "::") else HOST)
    print(f"Dashboard on https://{where}:{PORT} using a {st['mode']} certificate, valid until "
          f"{datetime.fromtimestamp(st['expires']):%d %b %Y}", flush=True)
    if st.get("error"):
        print(f"  Note: {st['error']}", flush=True)

    inner = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    inner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    inner.bind(("127.0.0.1", 0))
    inner_port = inner.getsockname()[1]

    config = uvicorn.Config(main.app, ssl_certfile=str(crt), ssl_keyfile=str(key),
                            log_level=LOG_LEVEL, access_log=False, proxy_headers=False, server_header=False)
    server = uvicorn.Server(config)
    tls.set_context_getter(lambda: config.ssl)
    tls.start_renewal()

    # `docker kill -s HUP homelab-dash` re-reads the certificate without a restart
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGHUP, lambda: loop.run_in_executor(None, tls.refresh))
    except (NotImplementedError, RuntimeError):
        pass

    front = await asyncio.start_server(front_handler(inner_port), HOST, PORT, reuse_address=True)
    front_task = asyncio.create_task(front.serve_forever())
    try:
        await server.serve(sockets=[inner])
    finally:
        front.close()
        front_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)

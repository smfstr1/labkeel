"""The plain-HTTP front door (serve.py): anyone who types http:// is sent to https://, and nothing they put in
the request can change where they're sent or what the little redirect page says."""
import serve


def test_sent_to_https_with_the_same_path():
    assert serve._redirect_target("homelab:8787", "/files?x=1") == f"https://homelab:{serve.PORT}/files?x=1"


def test_odd_hosts_and_paths_are_made_safe():
    assert serve._redirect_target("evil.example/path", "/").startswith("https://localhost")   # not a host name
    assert serve._redirect_target("homelab", "//evil.example/").endswith(f":{serve.PORT}/")  # no protocol-relative
    loc = serve._redirect_target("homelab", '/a"><script>alert(1)</script>\nSet-Cookie: x=1')
    assert "\n" not in loc and "<" not in loc and '"' not in loc and " " not in loc


def test_inner_port_answers_without_nagle_delay():
    """asyncio only sets TCP_NODELAY on sockets made with IPPROTO_TCP; without it each response sat ~40 ms."""
    import asyncio
    import socket

    async def go():
        loop = asyncio.get_running_loop()
        got = loop.create_future()

        class Capture(asyncio.Protocol):
            def connection_made(self, transport):
                got.set_result(transport.get_extra_info("socket").getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
                transport.close()

        sock = serve._inner_socket()
        server = await loop.create_server(Capture, sock=sock)
        _, w = await asyncio.open_connection("127.0.0.1", sock.getsockname()[1])
        try:
            return await asyncio.wait_for(got, 5)
        finally:
            w.close()
            server.close()
    assert asyncio.run(go()) == 1


def test_tls_is_1_2_or_newer_with_aead_ciphers_only(tmp_path):
    """Whatever the machine's OpenSSL would allow: TLS 1.0, 1.1 and the old CBC ciphers are refused."""
    import socket
    import ssl
    import threading
    import tls
    from test_servers import _self_signed
    _self_signed(tmp_path)
    server = tls.harden(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    server.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    lsock = socket.create_server(("127.0.0.1", 0))
    port = lsock.getsockname()[1]

    def serve():
        while True:
            try:
                s, _ = lsock.accept()
            except OSError:
                return
            try:
                with server.wrap_socket(s, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass
    threading.Thread(target=serve, daemon=True).start()

    def hello(version, ciphers=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        ctx.minimum_version = ctx.maximum_version = version
        if ciphers:
            ctx.set_ciphers(ciphers + ":@SECLEVEL=0")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s, ctx.wrap_socket(s) as t:
                return t.version()
        except (ssl.SSLError, OSError):
            return None
    try:
        assert hello(ssl.TLSVersion.TLSv1_3) == "TLSv1.3"
        assert hello(ssl.TLSVersion.TLSv1_2) == "TLSv1.2"
        assert hello(ssl.TLSVersion.TLSv1_2, "ECDHE-ECDSA-AES128-SHA") is None     # CBC with SHA-1
        assert hello(ssl.TLSVersion.TLSv1_2, "AES128-GCM-SHA256") is None          # no forward secrecy
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert hello(ssl.TLSVersion.TLSv1_1) is None
    finally:
        lsock.close()

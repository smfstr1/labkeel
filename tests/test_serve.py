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

"""Docker health check: is the dashboard answering? Works with HTTPS on or off."""
import ssl
import sys
import urllib.request

import env

host = env.text("LABKEEL_LISTEN_ADDRESS", "0.0.0.0")  # only read, to work out where to knock  # nosec B104
host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # "listening everywhere" → ask locally  # nosec B104
port = env.number("LABKEEL_PORT", 8787)
scheme = "http" if env.text("LABKEEL_HTTPS", "auto").strip().lower() == "off" else "https"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE  # only checking that it answers; the certificate is for the real name
try:
    with urllib.request.urlopen(f"{scheme}://{host}:{port}/api/health", timeout=5, context=ctx) as r:  # http(s) only  # nosec B310
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)

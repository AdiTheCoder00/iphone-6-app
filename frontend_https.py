# Serves the Companion PWA over HTTPS so iOS Safari unlocks getUserMedia
# (tap-to-talk) and the service worker. Requires the locally-trusted cert
# chain in certs/ (see README). The phone must trust companion-ca.crt first.
import http.server
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CERT_DIR = ROOT / "certs"
PORT = 8443

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(certfile=str(CERT_DIR / "companion-server.crt"),
                    keyfile=str(CERT_DIR / "companion-server.key"))
server.socket = ctx.wrap_socket(server.socket, server_side=True)
print("Companion PWA over https://0.0.0.0:%d (certs: %s)" % (PORT, CERT_DIR), flush=True)
server.serve_forever()
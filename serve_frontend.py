"""Serves the Companion PWA over HTTP or HTTPS with a strict file whitelist.

The repo root also holds certs/ (private keys), backend/data/ (the
conversation database) and backend/venv — a plain `python -m http.server` on
the root exposes all of those to anyone on the LAN, including the CA private
key that the phone trusts as a root CA. This server only ever serves the
handful of files the PWA actually needs; everything else answers 404, and no
directory listing is ever generated.

Usage:
    python serve_frontend.py --port 8080                   # plain HTTP
    python serve_frontend.py --port 8443 --cert-dir certs  # HTTPS

HTTPS is what unlocks getUserMedia (tap-to-talk) and the service worker on
iOS; it needs the locally-trusted chain in certs/ (see README) and the phone
must trust companion-ca.crt first.
"""

import argparse
import ssl
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The complete set of paths the PWA can request. Anything else — certs/,
# backend/, .git/ — answers 404.
ALLOWED_PATHS = {
    "/companion.html",
    "/qrcode.js",
    "/sw.js",
    "/manifest.json",
    "/design/mockups.html",
    "/icons/icon-180.png",
    "/icons/icon-192.png",
    "/icons/icon-512.png",
    "/icons/icon-maskable-512.png",
}

# Unchanging assets: the browser may hold these without revalidating.
CACHE_LONG = {
    "/qrcode.js",
    "/icons/icon-180.png",
    "/icons/icon-192.png",
    "/icons/icon-512.png",
    "/icons/icon-maskable-512.png",
}
# The shell itself: an edited companion.html must win over any cache.
CACHE_NO = {"/companion.html", "/sw.js", "/manifest.json"}


class CompanionHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _guarded(self) -> bool:
        """Decide whether this request may be served.

        Returns True to continue with the normal handler, False when a
        response (redirect or 404) has already been sent.
        """
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/companion.html")
            self.end_headers()
            return False
        if path not in ALLOWED_PATHS:
            self.send_error(404, "Not in the PWA whitelist")
            return False
        return True

    def do_GET(self):
        if self._guarded():
            super().do_GET()

    def do_HEAD(self):
        if self._guarded():
            super().do_HEAD()

    def end_headers(self):
        path = urllib.parse.urlparse(self.path).path
        if path in CACHE_NO:
            self.send_header("Cache-Control", "no-cache")
        elif path in CACHE_LONG:
            self.send_header("Cache-Control", "public, max-age=86400")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def serve(port: int, cert_dir: Path | None) -> None:
    if cert_dir is not None:
        if not cert_dir.is_absolute():
            cert_dir = ROOT / cert_dir
        cert = cert_dir / "companion-server.crt"
        key = cert_dir / "companion-server.key"
        if not cert.is_file() or not key.is_file():
            sys.exit(
                f"certificate files missing in {cert_dir} — generate the local "
                "chain first (see README, 'Local HTTPS certs')"
            )

    server = ThreadingHTTPServer(("0.0.0.0", port), CompanionHandler)

    if cert_dir is not None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    scheme = "https" if cert_dir is not None else "http"
    print(
        f"Companion PWA over {scheme}://0.0.0.0:{port} "
        f"(serving only the PWA whitelist, certs={cert_dir or 'none'})",
        flush=True,
    )
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the Companion PWA from a strict file whitelist."
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--cert-dir",
        type=Path,
        default=None,
        help="Serve HTTPS using the companion-server.{crt,key} pair in this directory",
    )
    args = parser.parse_args()
    serve(args.port, args.cert_dir)


if __name__ == "__main__":
    main()
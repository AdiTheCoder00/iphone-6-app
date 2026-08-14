# Serves the Companion PWA over HTTPS so iOS Safari unlocks getUserMedia
# (tap-to-talk) and the service worker. Requires the locally-trusted cert
# chain in certs/ (see README). The phone must trust companion-ca.crt first.
#
# Thin entry point over serve_frontend.py, which applies the strict file
# whitelist (certs/, backend/ and everything else outside the PWA answers
# 404). The phone opens https://<computer-LAN-IP>:8443/companion.html.
from pathlib import Path

from serve_frontend import serve

serve(port=8443, cert_dir=Path(__file__).resolve().parent / "certs")
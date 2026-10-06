"""display-hub HTTP service: `python3 -m hub`.

Two listeners in one process:
  PORT (8080)        devices: read-only paths (core.py, /weather/..., /aircraft/...).
                     Published on the LAN, because an ESP32 can't join the tailnet.
  ADMIN_PORT (8081)  people: the admin page and its API (admin.py), plus everything above.
                     Exposed ONLY through the DockTail VIP, never on the LAN.
Every app module provides ID, view_summary(), start(), health(), handle(h, path, query)
and preview_png().
"""
import json, os, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import admin, aircraft, airlist, core, metra, mqtt, weather

PORT = int(os.environ.get("PORT", "8080"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8081"))
APPS = [weather, aircraft, airlist, metra]  # first is the default app

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        # a client hanging up mid-response (health checks, a device losing WiFi)
        # is routine -- don't dump a traceback into the container log for it
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return True
    def json(self, obj, code=200):
        return self.send(json.dumps(obj).encode(), code=code)

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/") or "/"
        q = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}
        if p == "/":
            return self.json({"apps": [a.ID for a in APPS], "admin": "https://displays.swallow-spectrum.ts.net",
                              "paths": ["/device/hello", "/device/<id>/state", "/devices.json",
                                        "/firmware.json", "/healthz"] + ["/%s/..." % a.ID for a in APPS]})
        if p == "/healthz":
            per = {a.ID: a.health() for a in APPS}
            ok = all(v[0] for v in per.values())
            return self.json({"ok": ok, **{k: v[1] for k, v in per.items()}}, 200 if ok else 503)
        if core.handle(self, p, q, APPS):
            return
        for a in APPS:
            if a.handle(self, p, q):
                return
        self.json({"error": "not found"}, 404)

class AdminH(H):
    """The admin listener: the page and API first, then every device path."""
    def _admin(self, method):
        # unquote: the page escapes device ids (MACs: "20%3A6e%3A...")
        p = urllib.parse.unquote(self.path.split("?")[0]).rstrip("/") or "/"
        q = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        return admin.handle(self, method, p, q, body, APPS)
    def do_GET(self):
        if not self._admin("GET"): super().do_GET()
    def do_POST(self):
        if not self._admin("POST"): self.json({"error": "not found"}, 404)
    def do_PUT(self):
        if not self._admin("PUT"): self.json({"error": "not found"}, 404)
    def do_DELETE(self):
        if not self._admin("DELETE"): self.json({"error": "not found"}, 404)

if __name__ == "__main__":
    for a in APPS:
        a.start()
    mqtt.start(APPS)                    # HA discovery + control; idle until a broker is set
    threading.Thread(target=Server(("0.0.0.0", ADMIN_PORT), AdminH).serve_forever, daemon=True).start()
    print("display-hub serving devices on :%d, admin on :%d  apps=%s"
          % (PORT, ADMIN_PORT, ",".join(a.ID for a in APPS)), flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()

"""display-hub HTTP service: `python3 -m hub`.

Core device paths live in core.py; each app owns a path prefix (/weather/...).
Every app module provides ID, views(), start(), health() and handle(h, path, query).
"""
import json, os, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import aircraft, core, weather

PORT = int(os.environ.get("PORT", "8080"))
APPS = [weather, aircraft]  # first is the default app

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
            return self.json({"apps": [a.ID for a in APPS],
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

if __name__ == "__main__":
    for a in APPS:
        a.start()
    print("display-hub serving on :%d  apps=%s" % (PORT, ",".join(a.ID for a in APPS)), flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()

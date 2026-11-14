#!/usr/bin/env python3
"""
multi-model anon bridge router

  GET /health
  GET /?q=hello&model=gem                      -> gem/api.py
  GET /?q=hello&model=open                     -> open/api.py
  GET /?q=hello&bridge=gem&model=thinking      -> explicit route + model passthrough

  model=gem / model=open routes and is stripped before dispatch.
  model defaults to gem.

env:
  PORT   bind port (Render injects this)

render: python main.py
"""
import importlib.util
import inspect
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

MODULES = {
    "gem": "gem/api.py",
    "open": "open/api.py",
}
DEFAULT_MODEL = "gem"

_handlers = {}
_load_lock = threading.Lock()


# ---------------------------------------------------------------- log

def log(msg):
    sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- load

def load_handler(name, filepath):
    if not os.path.exists(filepath):
        log(f"missing file: {filepath}")
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"bridge_{name}", filepath)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        log(f"failed to load {filepath}: {e!r}")
        return None
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if (issubclass(obj, BaseHTTPRequestHandler)
                and obj is not BaseHTTPRequestHandler
                and obj.__module__ == mod.__name__):
            return obj
    log(f"no handler class found in {filepath}")
    return None


def get_handler(name):
    cls = _handlers.get(name)
    if cls:
        return cls
    with _load_lock:
        cls = _handlers.get(name)
        if cls:
            return cls
        cls = load_handler(name, MODULES[name])
        if cls:
            _handlers[name] = cls
    return cls


# ---------------------------------------------------------------- http

class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bridge-router"

    def log_message(self, fmt, *a):
        fwd = self.headers.get("X-Forwarded-For", "")
        who = fwd.split(",")[0].strip() or self.address_string()
        log(f"{who} {fmt % a}")

    def _plain(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)

        if parsed.path == "/health":
            return self._plain(200, "ok")

        if parsed.path == "/":
            lines = [f"model={k}  ->  {v}" for k, v in MODULES.items()]
            lines += ["", "GET /?q=hello&model=gem"]
            return self._plain(200, "\n".join(lines))

        bridge = qs.pop("bridge", [None])[0]
        if bridge is None:
            guess = (qs.get("model", [None])[0] or "").lower()
            if guess in MODULES:
                qs.pop("model")
                bridge = guess
            else:
                bridge = DEFAULT_MODEL
        bridge = bridge.lower()

        if bridge not in MODULES:
            return self._plain(400, f"unknown route '{bridge}'; use {', '.join(MODULES)}")

        cls = get_handler(bridge)
        if not cls:
            return self._plain(500, f"handler for '{bridge}' unavailable")

        self.path = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

        # bypass __init__: same socket, no second lifecycle, no reparse
        h = cls.__new__(cls)
        h.__dict__.update(self.__dict__)

        try:
            if self.command == "GET":
                h.do_GET()
            elif self.command == "POST":
                h.do_POST()
            else:
                self._plain(405, "method not allowed")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            log(f"error on '{bridge}': {e!r}")
            self.close_connection = True
            try:
                self._plain(500, f"handler error: {e}")
            except Exception:
                pass
        finally:
            self.close_connection = bool(h.close_connection or self.close_connection)


def run():
    port = int(os.environ.get("PORT") or 8000)
    for name in MODULES:
        log(f"{name}: {'ready' if get_handler(name) else 'UNAVAILABLE'}  ({MODULES[name]})")
    srv = ThreadingHTTPServer(("0.0.0.0", port), RouterHandler)
    srv.daemon_threads = True
    log(f"listening on 0.0.0.0:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()

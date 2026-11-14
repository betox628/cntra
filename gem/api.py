#!/usr/bin/env python3
"""
gemini anon bridge -> http api

  GET /?q=hello
  GET /?q=hello&session=<id>                (continue a thread)
  GET /?q=hello&model=thinking&stream=1     (raw text stream)
  GET /health

pip install requests
python bridge.py --host 0.0.0.0 --port 8000
"""
import argparse
import json
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

GEMINI = "https://gemini.google.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MODES = {"flash": 1, "thinking": 2, "pro": 3, "auto": 4, "lite": 6}
THINK = {"flash": 4, "thinking": 0, "pro": 4, "auto": 4, "lite": 4}

BUILD_FALLBACK = "boq_assistant-bard-web-server_20260716.08_p0"
BUILD_TTL = 3600
BUILD = {"bl": "", "ts": 0.0}
BUILD_LOCK = threading.Lock()

SESSIONS = {}
SESS_LOCK = threading.Lock()
MAX_TURNS = 10

API_TOKEN = os.environ.get("API_TOKEN")  # optional: require ?token=
COOKIE = None                            # optional: signed-in cookie header
KEEP_CODE = False                        # keep tool traces as markdown
_local = threading.local()


# ---------------------------------------------------------------- build

def fetch_build(s):
    r = s.get(GEMINI + "/app", headers={"User-Agent": UA}, timeout=30)
    m = re.search(r"(boq_assistant-bard-web-server_[\d._]+p\d+)", r.text)
    return m.group(1) if m else ""


def get_build(s, force=False):
    with BUILD_LOCK:
        fresh = BUILD["bl"] and (time.time() - BUILD["ts"] < BUILD_TTL)
        if fresh and not force:
            return BUILD["bl"]
        try:
            bl = fetch_build(s)
        except Exception:
            bl = ""
        if bl:
            BUILD.update(bl=bl, ts=time.time())
            return bl
        return BUILD["bl"] or BUILD_FALLBACK


# ---------------------------------------------------------------- payload

def get_session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        _local.s = s
    return s


def build_headers():
    h = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "*/*",
        "Origin": GEMINI,
        "Referer": GEMINI + "/app",
        "X-Same-Domain": "1",
    }
    if COOKIE:
        h["Cookie"] = COOKIE
    return h


def build_payload(prompt, mode, think):
    inner = [None] * 102
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]           # persist to history; [1] + inner[45]=1 for temporary
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = mode
    return urllib.parse.urlencode(
        {"f.req": json.dumps([None, json.dumps(inner)])})


def extract_texts(line):
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        inner = json.loads(json.loads(line)[0][2])
    except (json.JSONDecodeError, IndexError, TypeError):
        return []
    out = []
    if isinstance(inner, list) and len(inner) > 4 and inner[4]:
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and isinstance(part[1], list):
                out += [t for t in part[1] if isinstance(t, str) and t]
    return out


# ---------------------------------------------------------------- clean

CODE_FENCE = re.compile(
    r"```[a-z]*\?code_(?:reference|stdout)(?:&[^\n]*)?\n.*?```\n?", re.DOTALL)
CODE_FENCE_KEEP = re.compile(
    r"```([a-z]*)\?code_(?:reference|stdout)(?:&[^\n]*)?")
CARD_CONTENT = re.compile(r"https?://googleusercontent\.com/card_content/\d+\n?")


def clean_text(text, strip=True):
    if KEEP_CODE:
        text = CODE_FENCE_KEEP.sub(lambda m: "```" + (m.group(1) or ""), text)
    else:
        text = CODE_FENCE.sub("", text)
    text = CARD_CONTENT.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() if strip else text


def split_safe(text):
    """Hold back a trailing unterminated ``` so fences are cleaned whole."""
    if text.count("```") % 2 == 0:
        return text
    return text[:text.rfind("```")]


# ---------------------------------------------------------------- chat

def open_chat(prompt, mode, think):
    """Prepare + fire the request. Returns the streaming response object."""
    s = get_session()
    url = f"{GEMINI}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
    params = {
        "bl": get_build(s),
        "hl": "en",
        "_reqid": str(random.randint(100000, 999999)),
        "rt": "c",
    }
    resp = s.post(url, params=params, data=build_payload(prompt, mode, think),
                  headers=build_headers(), stream=True, timeout=300)
    if resp.status_code >= 400:                   # stale build -> refresh once
        resp.close()
        params["bl"] = get_build(s, force=True)
        resp = s.post(url, params=params, data=build_payload(prompt, mode, think),
                      headers=build_headers(), stream=True, timeout=300)
    if resp.status_code >= 400:
        raise RuntimeError(f"upstream {resp.status_code}: {resp.text[:400]}")
    return resp


def iter_frames(resp):
    """Yield cleaned assistant text deltas as they arrive."""
    raw_acc = ""
    emitted = ""
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        if "BardErrorInfo" in line:
            m = re.search(r"BardErrorInfo\s*\[(\d+)\]", line)
            raise RuntimeError(
                f"upstream rejected: BardErrorInfo [{m.group(1) if m else '?'}]")
        for t in extract_texts(line):
            if t == raw_acc or raw_acc.startswith(t):
                continue
            raw_acc = t
            c = clean_text(split_safe(t), strip=False)
            if not c.startswith(emitted):
                emitted = c
                continue
            if len(c) > len(emitted):
                yield c[len(emitted):]
                emitted = c


def history_prompt(sid, question):
    if not sid:
        return question
    with SESS_LOCK:
        turns = list(SESSIONS.get(sid, []))
    if not turns:
        return question
    body = "\n\n".join(f"User: {u}\nAssistant: {a}" for u, a in turns)
    return f"{body}\n\nUser: {question}\nAssistant:"


def remember(sid, q, a):
    if not sid:
        return
    with SESS_LOCK:
        SESSIONS[sid] = (SESSIONS.get(sid, []) + [(q, a)])[-MAX_TURNS:]


def chat(prompt, mode, think):
    resp = open_chat(prompt, mode, think)
    try:
        return "".join(iter_frames(resp)).strip()
    finally:
        resp.close()


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gemini-bridge"

    def log_message(self, fmt, *a):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % a}")

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self):
        u = urlparse(self.path)
        p = parse_qs(u.query)
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if raw:
                try:
                    if "json" in (self.headers.get("Content-Type") or ""):
                        p.update({k: [v] if not isinstance(v, list) else v
                                  for k, v in json.loads(raw).items()})
                    else:
                        p.update(parse_qs(raw.decode("utf-8", "replace")))
                except Exception:
                    pass
        return u.path, p

    def do_GET(self):
        path, p = self._params()

        if path == "/health":
            return self._send_json(200, {"ok": True})

        if API_TOKEN and p.get("token", [""])[0] != API_TOKEN:
            return self._send_json(401, {"error": "bad token"})

        q = (p.get("q", [""])[0] or "").strip()
        if not q:
            return self._send_json(400, {"error": "missing ?q="})

        given = p.get("session", [""])[0]
        sid = given or str(uuid.uuid4())
        name = p.get("model", ["flash"])[0] or "flash"
        if name not in MODES:
            name = "flash"
        stream = p.get("stream", ["0"])[0] in ("1", "true", "yes")
        prompt = history_prompt(given, q)

        try:
            resp = open_chat(prompt, MODES[name], THINK[name])
        except Exception as e:
            return self._send_json(502, {"error": str(e)})

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            acc = []
            try:
                for chunk in iter_frames(resp):
                    acc.append(chunk)
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self.wfile.write(f"\n[error] {e}\n".encode())
                except Exception:
                    pass
            finally:
                resp.close()
                remember(given, q, "".join(acc).strip())
            return

        try:
            text = "".join(iter_frames(resp)).strip()
        except Exception as e:
            return self._send_json(502, {"error": str(e)})
        finally:
            resp.close()

        remember(given, q, text)
        text.replace("$"," ")
        text.replace("/", " ")
        return self._send_json(200, {
            "msg": text,
            "session": sid,
        })

    do_POST = do_GET


def main():
    global COOKIE, KEEP_CODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cookie-file", help="raw cookie header; needed for real Pro")
    ap.add_argument("--keep-code", action="store_true",
                    help="keep tool traces as markdown, drop only the ?query noise")
    a = ap.parse_args()

    if a.cookie_file:
        with open(a.cookie_file) as f:
            COOKIE = f.read().strip() or None
    KEEP_CODE = a.keep_code

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"listening on http://{a.host}:{a.port}  ->  /?q=hello")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

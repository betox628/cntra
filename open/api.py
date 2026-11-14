#!/usr/bin/env python3
"""
chatgpt anon bridge -> http api

  GET /?q=hello
  GET /?q=hello&conversation_id=<id>&parent_id=<msg_id>   (continue a thread)
  GET /?q=hello&model=auto&stream=1                       (raw text/event stream)
  GET /health

pip install requests
python bridge.py --host 0.0.0.0 --port 8000
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

CHATGPT = "https://chatgpt.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Paste your Cookie-Editor JSON export verbatim between the triple quotes
COOKIES_JSON = r"""
[
  {"domain":"chatgpt.com","name":"__cflb","value":"0H28vzvP5FJafnkHxih2nYMWHNDJXXQNd9XpedyVbKZ"},
  {"domain":".chatgpt.com","name":"__cf_bm","value":"WIjaAznqmGmzXjoFZ3C2_kfDrX_RNCzENoIfQ4i0sLo-1793039090.9313464-1.0.1.1-e1m8EMiDPnrwb36SV2A5O01R8Gos5fD2ZBUwsUo5TVOdC5DhNYIrHQltZVyN3cmSUGSuMGIXrXLDJyFCLZxNgPAABkLpRXrXRX9sdtp3QpdiahOJatdtk_mIW8qTYvGI"},
  {"domain":".chatgpt.com","name":"oai-did","value":"478c50c2-da7c-4aa3-b3c3-36821c92610d"}
]
"""

CFG = json.load(open("sentinel.json"))
CORES = [8, 16, 24, 32]

API_TOKEN = os.environ.get("API_TOKEN")  # optional: require ?token=
BUILD_TTL = 600
BUILD = {"dpl": "", "scripts": [], "ts": 0.0}
BUILD_LOCK = threading.Lock()
_local = threading.local()


# ---------------------------------------------------------------- cookies

def load_jar():
    jar, device_id = {}, None
    for c in json.loads(COOKIES_JSON):
        jar[c["name"]] = c["value"]
        if c["name"] == "oai-did":
            device_id = c["value"]
    return jar, device_id


def get_session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        _local.s = s
    jar, _ = load_jar()
    s.cookies.update(jar)
    return s


# ---------------------------------------------------------------- sentinel

def parse_time():
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def fetch_build(s):
    r = s.get(CHATGPT + "/", headers={"User-Agent": UA}, timeout=30)
    dpl = re.search(r'<html[^>]*data-build="([^"]*)"', r.text)
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', r.text)
    return (dpl.group(1) if dpl else ""), scripts


def get_build(s, force=False):
    with BUILD_LOCK:
        fresh = BUILD["scripts"] and (time.time() - BUILD["ts"] < BUILD_TTL)
        if fresh and not force:
            return BUILD["dpl"], BUILD["scripts"]
        dpl, scripts = fetch_build(s)
        BUILD.update(dpl=dpl, scripts=scripts, ts=time.time())
        return dpl, scripts


def build_config(scripts, dpl):
    return [
        random.choice([1920 + 1080, 2560 + 1440, 1920 + 1200, 2560 + 1600]),
        parse_time(), 4294705152, 0, UA,
        random.choice(scripts) if scripts else "", dpl,
        "en-US", "en-US,es-US,en,es", 0,
        random.choice(CFG["navigator_key"]),
        random.choice(CFG["document_key"]),
        random.choice(CFG["window_key"]),
        time.perf_counter() * 1000, str(uuid.uuid4()), "",
        random.choice(CORES),
        time.time() * 1000 - time.perf_counter() * 1000,
    ]


def solve_pow(seed, diff, config):
    n = len(diff)
    target = bytes.fromhex(diff)
    p1 = (json.dumps(config[:3], separators=(',', ':'), ensure_ascii=False)[:-1] + ',').encode()
    p2 = (',' + json.dumps(config[4:9], separators=(',', ':'), ensure_ascii=False)[1:-1] + ',').encode()
    p3 = (',' + json.dumps(config[10:], separators=(',', ':'), ensure_ascii=False)[1:]).encode()
    seed_b = seed.encode()
    for i in range(500_000):
        blob = p1 + str(i).encode() + p2 + str(i >> 1).encode() + p3
        enc = base64.b64encode(blob)
        if hashlib.sha3_512(seed_b + enc).digest()[:n] <= target:
            return enc.decode()
    raise RuntimeError("PoW failed")


# ---------------------------------------------------------------- chat

def open_chat(prompt, conversation_id=None, parent_id=None, model="auto"):
    """Prepare + fire the request. Returns the streaming response object."""
    _, device_id = load_jar()
    s = get_session()

    base = f"{CHATGPT}/backend-anon"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": CHATGPT,
        "Referer": CHATGPT + "/",
        "oai-language": "en-US",
        "oai-device-id": device_id or "",
    }

    dpl, scripts = get_build(s)
    config = build_config(scripts, dpl)

    req_token = "gAAAAAC" + solve_pow(format(random.random()), "0fffff", config)
    r = s.post(f"{base}/sentinel/chat-requirements", headers=headers,
               json={"p": req_token}, timeout=30)
    if r.status_code >= 400:                      # stale build -> refresh once
        dpl, scripts = get_build(s, force=True)
        config = build_config(scripts, dpl)
        req_token = "gAAAAAC" + solve_pow(format(random.random()), "0fffff", config)
        r = s.post(f"{base}/sentinel/chat-requirements", headers=headers,
                   json={"p": req_token}, timeout=30)
    r.raise_for_status()
    req = r.json()

    chat_token = req["token"]
    proof = None
    if req.get("proofofwork", {}).get("required"):
        pw = req["proofofwork"]
        proof = "gAAAAAB" + solve_pow(pw["seed"], pw["difficulty"], config)

    headers.update({
        "Accept": "text/event-stream",
        "openai-sentinel-chat-requirements-token": chat_token,
    })
    if proof:
        headers["openai-sentinel-proof-token"] = proof

    payload = {
        "action": "next",
        "messages": [{
            "id": str(uuid.uuid4()),
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [prompt]},
        }],
        "parent_message_id": parent_id or str(uuid.uuid4()),
        "model": model,
        "timezone_offset_min": -300,
        "history_and_training_disabled": False,
        "conversation_mode": {"kind": "primary_assistant"},
        "force_use_sse": True,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    resp = s.post(f"{base}/conversation", headers=headers, json=payload,
                  stream=True, timeout=300)
    if resp.status_code >= 400:
        raise RuntimeError(f"upstream {resp.status_code}: {resp.text[:400]}")
    return resp


def iter_frames(resp):
    """Yield assistant text deltas as they arrive."""
    last_text = ""
    conv = pid = None
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8", "replace")
        if not line.startswith("data: "):
            continue
        d = line[6:]
        if d == "[DONE]":
            break
        try:
            obj = json.loads(d)
        except json.JSONDecodeError:
            continue
        m = obj.get("message")
        if not (isinstance(m, dict)
                and m.get("author", {}).get("role") == "assistant"
                and m.get("content", {}).get("parts")):
            continue
        text = m["content"]["parts"][0] or ""
        if text.startswith(last_text) and len(text) > len(last_text):
            yield text[len(last_text):]
        elif text != last_text:
            yield text
        last_text = text
        conv = obj.get("conversation_id", conv)
        pid = m.get("id", pid)
    _local.last_ids = (conv, pid)


def chat(prompt, conversation_id=None, parent_id=None, model="auto"):
    resp = open_chat(prompt, conversation_id, parent_id, model)
    try:
        text = "".join(iter_frames(resp))
    finally:
        resp.close()
    conv, pid = getattr(_local, "last_ids", (None, None))
    if not text:
        raise RuntimeError("empty stream")
    return text, conv, pid


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cgpt-bridge"

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

        conv = p.get("conversation_id", [None])[0] or None
        pid = p.get("parent_id", [None])[0] or None
        model = p.get("model", ["auto"])[0] or "auto"
        stream = p.get("stream", ["0"])[0] in ("1", "true", "yes")

        try:
            resp = open_chat(q, conv, pid, model)
        except Exception as e:
            return self._send_json(502, {"error": str(e)})

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in iter_frames(resp):
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                resp.close()
            return

        try:
            text = "".join(iter_frames(resp))
        except Exception as e:
            return self._send_json(502, {"error": str(e)})
        finally:
            resp.close()

        c, m = getattr(_local, "last_ids", (None, None))
        return self._send_json(200, {
            "msg": text,
        })

    do_POST = do_GET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"listening on http://{a.host}:{a.port}  ->  /?q=hello")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
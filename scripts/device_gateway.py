#!/usr/bin/env python3
"""Small, dependency-free LAN gateway for laptop/iPhone/Android inference.

Each backend is an independent OpenAI-compatible llama-server. This gateway does
not split a model across devices. Keep the gateway and backends on a trusted LAN.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 32 * 1024
MAX_MESSAGES = 16
MAX_CONTENT = 4000
PAGE = Path(__file__).resolve().parent.parent / "web" / "device-gateway.html"


def backend_urls() -> dict[str, str]:
    result = {}
    for name in ("laptop", "android"):
        value = os.environ.get(f"SHARECOMPUTE_{name.upper()}_URL", "").rstrip("/")
        if value:
            if not value.startswith("http://") or "/" in value[7:]:
                raise ValueError(f"SHARECOMPUTE_{name.upper()}_URL must be an http://host:port URL")
            result[name] = value
    return result


def parse_chat(raw: bytes, backends: dict[str, str]) -> tuple[str, list[dict], int]:
    try:
        body = json.loads(raw)
        name = body["backend"]
        messages = body["messages"]
        max_tokens = body.get("max_tokens", 128)
        if name not in backends or not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
            raise ValueError("Invalid backend or message count")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 512:
            raise ValueError("max_tokens must be 1–512")
        if any(
            not isinstance(m, dict)
            or m.get("role") not in ("user", "assistant")
            or not isinstance(m.get("content"), str)
            or not 1 <= len(m["content"]) <= MAX_CONTENT
            for m in messages
        ) or messages[-1]["role"] != "user":
            raise ValueError("Messages must be bounded text ending in a user message")
        return name, [{"role": m["role"], "content": m["content"]} for m in messages], max_tokens
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid chat request") from exc


def upstream(url: str, path: str, *, payload: dict | None = None, key: str = "", timeout: int = 10) -> dict:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def model_id(url: str, key: str) -> str:
    models = upstream(url, "/v1/models", key=key, timeout=5)["data"]
    if not isinstance(models, list) or not models or not isinstance(models[0].get("id"), str):
        raise ValueError("No model loaded")
    return models[0]["id"]


def make_handler(backends: dict[str, str], access_token: str, backend_keys: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log chat text or access tokens.
            print("gateway: " + fmt % args, file=sys.stderr)

        def reply(self, status: int, body: dict):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            supplied = self.headers.get("X-ShareCompute-Token", "")
            if not hmac.compare_digest(supplied, access_token):
                self.reply(401, {"error": "Enter the access code shown on the laptop"})
                return False
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin != f"http://{host}":
                self.reply(403, {"error": "Cross-origin requests are blocked"})
                return False
            return True

        def do_GET(self):
            if self.path == "/":
                page = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if self.path != "/api/status":
                self.reply(404, {"error": "Not found"})
                return
            if not self.authorized():
                return
            statuses = {}
            for name, url in backends.items():
                try:
                    health = upstream(url, "/health", key=backend_keys[name], timeout=3)
                    ready = health.get("status") in ("ok", "ready")
                    if ready:
                        model_id(url, backend_keys[name])
                    statuses[name] = {"ready": ready, "detail": health.get("status", "unknown")}
                except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError, KeyError, TypeError, IndexError):
                    statuses[name] = {"ready": False, "detail": "unreachable"}
            self.reply(200, {"backends": statuses, "mode": "independent inference; no pooled RAM"})

        def do_POST(self):
            if self.path != "/api/chat":
                self.reply(404, {"error": "Not found"})
                return
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 1 or length > MAX_BODY:
                self.reply(413, {"error": "Request too large or empty"})
                return
            try:
                name, messages, max_tokens = parse_chat(self.rfile.read(length), backends)
            except ValueError as exc:
                self.reply(400, {"error": str(exc)})
                return
            try:
                payload = {"model": model_id(backends[name], backend_keys[name]), "messages": messages, "max_tokens": max_tokens, "stream": False}
                result = upstream(backends[name], "/v1/chat/completions", payload=payload, key=backend_keys[name], timeout=120)
                answer = result["choices"][0]["message"]["content"]
                if not isinstance(answer, str):
                    raise ValueError("Empty response from model")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                self.reply(502, {"error": f"{name} backend failed: {type(exc).__name__}"})
                return
            self.reply(200, {"backend": name, "answer": answer})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Use laptop LAN IP to allow phone access")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        backends = backend_urls()
    except ValueError as exc:
        parser.error(str(exc))
    if not backends:
        parser.error("Configure SHARECOMPUTE_LAPTOP_URL and/or SHARECOMPUTE_ANDROID_URL")
    token = os.environ.get("SHARECOMPUTE_ACCESS_TOKEN") or secrets.token_urlsafe(18)
    if len(token) < 16:
        parser.error("SHARECOMPUTE_ACCESS_TOKEN must be at least 16 characters")
    keys = {name: os.environ.get(f"SHARECOMPUTE_{name.upper()}_KEY", "") for name in backends}
    server = ThreadingHTTPServer((args.host, args.port), make_handler(backends, token, keys))
    print(f"Open http://{args.host}:{server.server_port}/ on your phones (same Wi-Fi).", flush=True)
    print(f"Access code: {token}", flush=True)
    print("Backends: " + ", ".join(backends) + " (independent, no pooled RAM)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

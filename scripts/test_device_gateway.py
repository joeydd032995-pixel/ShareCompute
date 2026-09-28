#!/usr/bin/env python3
"""End-to-end gateway check with a fake llama-server; no model download needed."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from device_gateway import make_handler, parse_chat  # noqa: E402


class FakeModel(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        payload = {"status": "ok"} if self.path == "/health" else {"data": [{"id": "test-model"}]}
        self.respond(200, payload)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append((self.path, data, self.headers.get("Authorization")))
        self.respond(200, {"choices": [{"message": {"content": "Hello from Android"}}]})

    def respond(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        cls.model_thread = serve(cls.model)
        url = f"http://127.0.0.1:{cls.model.server_port}"
        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), make_handler({"android": url}, "long-test-secret-123", {"android": "backend-secret"}))
        cls.gateway_thread = serve(cls.gateway)
        cls.base = f"http://127.0.0.1:{cls.gateway.server_port}"

    @classmethod
    def tearDownClass(cls):
        for server, thread in ((cls.gateway, cls.gateway_thread), (cls.model, cls.model_thread)):
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def request(self, path, data=None, token="long-test-secret-123", origin=None):
        headers = {"X-ShareCompute-Token": token}
        if origin:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_health_and_chat_on_android(self):
        code, body = self.request("/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(body["backends"]["android"]["ready"])
        code, body = self.request("/api/chat", {"backend": "android", "messages": [{"role": "user", "content": "Hi"}]})
        self.assertEqual((code, body["answer"]), (200, "Hello from Android"))
        path, sent, auth = FakeModel.received[-1]
        self.assertEqual((path, sent["model"], auth), ("/v1/chat/completions", "test-model", "Bearer backend-secret"))

    def test_auth_origin_and_bad_request(self):
        self.assertEqual(self.request("/api/status", token="wrong")[0], 401)
        self.assertEqual(self.request("/api/status", origin="http://other-site")[0], 403)
        self.assertEqual(self.request("/api/chat", {"backend": "unknown", "messages": []})[0], 400)
        self.assertEqual(self.request("/api/chat", {"backend": "android", "messages": [{"role": "user", "content": "x"}], "max_tokens": 999})[0], 400)

    def test_page_is_mobile_accessible(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            body = response.read().decode()
        self.assertIn('name="viewport"', body)
        self.assertIn('id="backend"', body)

    def test_parser_rejects_non_text(self):
        with self.assertRaises(ValueError):
            parse_chat(json.dumps({"backend": "android", "messages": [{"role": "user", "content": ["not text"]}]}).encode(), {"android": "http://localhost"})


if __name__ == "__main__":
    unittest.main()

"""Hermetic TypeSafe HTTPS fixture; no real account, secret or developer directory."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from bloxsmith_app.block_api import BlockRuntimeContext

KEY = "test-only-typesafe-credential"
REF = "secret://workspace/typesafe_test"
QUESTIONS = {
    "action": {"type": "noul", "instructions": "Does the message request an action?"},
    "team": {"type": "choice", "instructions": "Which team should handle this?",
             "criteria": {"code": "Code changes", "other": None}},
    "severity": {"type": "score", "instructions": "How severe is the issue?",
                 "criteria": [{"meaning": "Cosmetic"}, "Blocking"]},
}


def answer(questions):
    """Return the documented envelope for every question, including structured legends."""
    answers = {}
    for key, question in questions.items():
        kind = question["type"]
        if kind == "noul":
            answers[key] = {"type": kind, "noul": .93}
        elif kind == "choice":
            options = list(question["criteria"])
            answers[key] = {"type": kind, "choice": options[0], "confidence": 1,
                            "probabilities": {v: int(v == options[0]) for v in options}}
        else:
            answers[key] = {"type": kind, "score": 0, "confidence": 1,
                            "legend": {str(i): v for i, v in enumerate(question["criteria"])},
                            "probabilities": {str(i): int(i == 0) for i in range(len(question["criteria"]))}}
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 45, "output_tokens": 12}}


@contextmanager
def fake_api():
    """Intercept only api.typesafe.ai over a locally trusted TLS CONNECT proxy.

    Production sources/endpoint remain unchanged even in managed package tests.
    All fixture certificates, keys and proxy settings are temporary and test-only.
    """
    with TemporaryDirectory(prefix="jev-api-test-") as name:
        directory = Path(name)
        certificate, private_key = directory / "fixture.crt", directory / "fixture.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=api.typesafe.ai", "-addext", "subjectAltName=DNS:api.typesafe.ai",
                        "-keyout", str(private_key), "-out", str(certificate)], check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certificate, private_key)
        fixture = SimpleNamespace(directory=directory, calls=[], mode="ok", requests=threading.Event(),
                                  release=threading.Event(), disconnected=threading.Event())

        class Handler(BaseHTTPRequestHandler):
            """Serve a single bounded API response with controllable error cases."""

            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                if self.path != "api.typesafe.ai:443":
                    self.send_error(403)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                try:
                    self.connection = tls.wrap_socket(self.connection, server_side=True)
                    self.rfile = self.connection.makefile("rb")
                    self.wfile = self.connection.makefile("wb")
                    self.handle_one_request()
                except (OSError, ssl.SSLError):
                    pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                document = json.loads(raw)
                fixture.calls.append({"body": document, "path": self.path,
                                      "authorization": self.headers.get("Authorization")})
                fixture.requests.set()
                status, response = 200, answer(document["questions"])
                mode = fixture.mode
                if mode == "slow":
                    fixture.release.wait(timeout=8)
                if mode == "malformed":
                    response = {"model": "jev-1.13.0", "answers": {}, "usage": {}}
                if mode in {"401", "422", "429", "529", "redirect"}:
                    status = 307 if mode == "redirect" else int(mode)
                    response = {"error": KEY + " private request body must never escape"}
                if mode == "retry" and len(fixture.calls) == 1:
                    status, response = 429, {"error": "busy"}
                encoded = (b"x" * (513 * 1024) if mode == "large" else b"not-json" if mode == "invalid-json"
                           else json.dumps(response).encode())
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Retry-After", "0")
                    if mode == "redirect":
                        self.send_header("Location", "https://must-not-contact.invalid/collect")
                    self.end_headers()
                    self.wfile.write(encoded)
                    self.wfile.flush()
                except (BrokenPipeError, OSError):
                    fixture.disconnected.set()

        class Server(ThreadingHTTPServer):
            """Closing a cancelled TLS request is expected, not a fixture crash."""

            def handle_error(self, request, client_address):
                import sys
                if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError)):
                    fixture.disconnected.set()
                else:
                    super().handle_error(request, client_address)

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = {"HTTPS_PROXY": f"http://127.0.0.1:{server.server_port}",
                       "https_proxy": f"http://127.0.0.1:{server.server_port}",
                       "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
                       "SSL_CERT_FILE": str(certificate)}
        try:
            with patch.dict(os.environ, environment):
                yield fixture
        finally:
            fixture.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def context(directory, *, config=None, value="Please fix the code.", content_type="text/plain", services=None, mode="centralized"):
    """Build a public context without probing a real wallet or filesystem instance."""
    return BlockRuntimeContext(run_id="jev-test-run", node_id="jev-test", kind="jev", root_dir=directory,
        runtime_mode=mode, config={"api_key_ref": REF, "questions": QUESTIONS, "timeout_sec": 5, **(config or {})},
        inputs={1: value}, input_content_types={1: content_type},
        input_ports=(SimpleNamespace(id=1, name="state"),), output_ports=(SimpleNamespace(id=1, name="result"),),
        services={"resolve_secret": lambda ref: KEY if ref == REF else "", **(services or {})})


def node():
    """Create a version-pinned graph node with test-only wallet reference."""
    from blocs.jev.block import JevBlock
    result = JevBlock().build_node_payload(node_id="jev-test", position={"x": 340, "y": 140})
    result["block_version"] = "0.1.0"
    result["config"].update(api_key_ref=REF, questions=QUESTIONS, timeout_sec=8)
    return result

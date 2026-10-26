"""Opt-in Caddy reference tests against an isolated loopback TLS proxy.

Set CUSTODIAN_CADDY_BIN to a Caddy 2 executable. Only listeners, certificates,
admin/autosave settings and backend addresses change from the reference file.
No system configuration, public certificates or live services are touched.
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "install/templates/proxy/caddy/Caddyfile"
BINARY = os.environ.get("CUSTODIAN_CADDY_BIN")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CaddyFixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="custodian-caddy-")
        self.root = Path(self.tmp.name)
        self.port = free_port()
        self.http_port = free_port()
        self.process = None
        self.server = None
        self.thread = None
        self.log = None
        self.connected = threading.Event()
        self.records = []

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def start(self):
        fixture = self

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                # Record headers immediately: a backend that waits for the
                # whole body before recording would hide partial forwarding.
                record = [self.path, dict(self.headers), b""]
                fixture.records.append(record)
                self.connection.settimeout(2)
                try:
                    record[2] = self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(record[2])))
                    self.send_header("Cache-Control", "public")
                    self.end_headers()
                    self.wfile.write(record[2])
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            def get_request(self):
                connection = super().get_request()
                fixture.connected.set()
                return connection

        self.server = Server(("127.0.0.1", 0), Receiver)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(self.root / "key.pem"),
                "-out",
                str(self.root / "cert.pem"),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            capture_output=True,
            check=True,
        )
        self.context = ssl.create_default_context(cafile=str(self.root / "cert.pem"))
        config = CADDYFILE.read_text().replace(
            "{\n    servers {",
            "{\n    admin off\n    auto_https off\n    persist_config off\n    servers {",
            1,
        )
        config = config.replace(
            "\nDOMAIN {",
            f"\nhttps://127.0.0.1:{self.port} {{\n    bind 127.0.0.1\n"
            f'    tls "{self.root / "cert.pem"}" "{self.root / "key.pem"}"',
        ).replace(
            "\nhttp://DOMAIN {",
            f"\nhttp://127.0.0.1:{self.http_port} {{\n    bind 127.0.0.1",
        )
        config = config.replace("CUSTODIAN_COUNCIL_PORT", str(self.server.server_port))
        path = self.root / "Caddyfile"
        path.write_text(config)
        environment = dict(
            os.environ,
            XDG_DATA_HOME=str(self.root / "data"),
            XDG_CONFIG_HOME=str(self.root / "config"),
        )
        checked = subprocess.run(
            [BINARY, "validate", "--adapter", "caddyfile", "--config", str(path)],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if checked.returncode:
            raise AssertionError(checked.stdout + checked.stderr)
        self.log = (self.root / "caddy.log").open("wb")
        self.process = subprocess.Popen(
            [BINARY, "run", "--adapter", "caddyfile", "--config", str(path)],
            cwd=self.root,
            env=environment,
            stdout=self.log,
            stderr=self.log,
        )
        for _ in range(100):
            if self.process.poll() is not None:
                raise AssertionError((self.root / "caddy.log").read_text())
            try:
                with self.raw():
                    return self
            except OSError:
                time.sleep(0.03)
        raise AssertionError("fixture Caddy did not start")

    def __exit__(self, *args):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.log is not None:
            self.log.close()
        self.tmp.cleanup()

    def raw(self):
        return self.context.wrap_socket(
            socket.create_connection(("127.0.0.1", self.port), timeout=8),
            server_hostname="127.0.0.1",
        )

    def request(self, body):
        connection = http.client.HTTPSConnection(
            "127.0.0.1", self.port, context=self.context, timeout=8
        )
        try:
            connection.request(
                "POST",
                "/custodian/v1/council",
                body,
                {"Content-Type": "application/cbor"},
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


@unittest.skipUnless(
    BINARY and shutil.which("openssl"),
    "set CUSTODIAN_CADDY_BIN for isolated Caddy TLS integration",
)
class CustodianCaddyTests(unittest.TestCase):
    def test_complete_bodies_through_inclusive_limit(self):
        with CaddyFixture() as proxy:
            for size in (1, 4096, 65536):
                with self.subTest(size=size):
                    body = b"s" * size
                    status, headers, received = proxy.request(body)
                    self.assertEqual(status, 200)
                    self.assertEqual(received, body)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(proxy.records[-1][0], "/v1/council")
                    self.assertEqual(proxy.records[-1][2], body)

    def test_partial_upload_does_not_contact_backend_before_completion(self):
        with CaddyFixture() as proxy, proxy.raw() as sock:
            sock.sendall(
                b"POST /custodian/v1/council HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/cbor\r\nContent-Length: 65536\r\n\r\n"
                + b"s"
                * 4096
            )
            self.assertFalse(
                proxy.connected.wait(0.3), "partial upload contacted Custodian"
            )
            sock.sendall(b"s" * (65536 - 4096))
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"s" * 65536)
            self.assertEqual(len(proxy.records), 1)

    def test_oversized_upload_does_not_contact_backend(self):
        with CaddyFixture() as proxy:
            self.assertEqual(proxy.request(b"s" * 65537)[0], 413)
            self.assertFalse(
                proxy.connected.wait(0.3), "oversized upload contacted Custodian"
            )
            self.assertEqual(proxy.records, [])


if __name__ == "__main__":
    unittest.main()

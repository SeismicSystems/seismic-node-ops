"""Opt-in real OpenResty TLS tests on ephemeral loopback ports, without root.

Set CUSTODIAN_OPENRESTY_BIN to an OpenResty nginx executable. The production
renderer, route limits and Custodian Lua handler run unchanged. Only ACME is
stubbed (a fixture CA/certificate is used); unrelated node upstreams are redirected
to the same fixture receiver. No system configuration or services are touched.
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

from test_custodian_tls import ROOT, render

BINARY = os.environ.get("CUSTODIAN_OPENRESTY_BIN")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProxyFixture:
    def __init__(self, mode="custodian", backend_port=None):
        self.backend_port = backend_port
        self.tmp = tempfile.TemporaryDirectory(prefix="custodian-proxy-")
        self.root = Path(self.tmp.name)
        self.records = []
        self.port = free_port()
        self.http_port = free_port()
        self.process = None
        self.server = None
        self.thread = None
        self.mode = mode

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
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.records.append((self.path, dict(self.headers), body))
                status = 200
                if fixture.backend_port is not None:
                    conn = http.client.HTTPConnection(
                        "127.0.0.1", fixture.backend_port, timeout=5
                    )
                    try:
                        conn.request(
                            "POST",
                            self.path,
                            body=body,
                            headers={"Content-Type": "application/cbor"},
                        )
                        response = conn.getresponse()
                        status, body = response.status, response.read()
                    finally:
                        conn.close()
                self.send_response(status)
                self.send_header("Content-Type", "application/cbor")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
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
        lua = self.root / "lua"
        (lua / "resty").mkdir(parents=True)
        (self.root / "logs").mkdir()
        (lua / "resty/auto-ssl.lua").write_text(
            "local noop = function() end; return {new = function() return "
            "{set=noop, init=noop, init_worker=noop, ssl_certificate=noop, "
            "challenge_server=noop, hook_server=noop} end}\n"
        )
        shutil.copyfile(
            ROOT / "install/templates/openresty/lua/custodian.lua",
            lua / "custodian.lua",
        )
        # Existing non-Custodian auth is outside this fixture's scope.
        (lua / "rate_limit.lua").write_text("-- unrelated node-route limiter stub\n")
        (lua / "jwt_auth.lua").write_text("return ngx.exit(401)\n")
        # Exercise the production port substitution, rather than rewriting a
        # hardcoded Custodian upstream after rendering.
        config = render(
            self.mode, lua_dir=str(lua), custodian_port=self.server.server_port
        )
        config = config.replace("user nobody nogroup;", "").replace(
            "worker_processes auto;", "worker_processes 1;"
        )
        config = config.replace("listen 443 ssl;", f"listen 127.0.0.1:{self.port} ssl;")
        config = config.replace("listen 80;", f"listen 127.0.0.1:{self.http_port};")
        config = config.replace("127.0.0.1:8999", f"127.0.0.1:{free_port()}")
        config = config.replace(
            "/etc/ssl/resty-auto-ssl-fallback.crt", str(self.root / "cert.pem")
        )
        config = config.replace(
            "/etc/ssl/resty-auto-ssl-fallback.key", str(self.root / "key.pem")
        )
        for host in ("localhost", "127.0.0.1"):
            for port in (3000, 8545, 8546, 3030, 8552, 42069, 9090, 9001):
                config = config.replace(
                    f"http://{host}:{port}",
                    f"http://127.0.0.1:{self.server.server_port}",
                )
        config = "daemon off;\n" + config
        (self.root / "nginx.conf").write_text(config)
        checked = subprocess.run(
            [
                BINARY,
                "-t",
                "-p",
                str(self.root) + "/",
                "-c",
                str(self.root / "nginx.conf"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if checked.returncode:
            raise AssertionError(checked.stderr)
        self.process = subprocess.Popen(
            [BINARY, "-p", str(self.root) + "/", "-c", str(self.root / "nginx.conf")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if self.process.poll() is not None:
                raise AssertionError((self.root / "logs/error.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return self
            except OSError:
                time.sleep(0.03)
        raise AssertionError("fixture OpenResty did not start")

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
        self.tmp.cleanup()

    def request(
        self,
        path="/custodian/v1/council",
        body=b"fixture-secret",
        method="POST",
        headers=None,
        tls=True,
        source="127.0.0.1",
    ):
        conn = (
            http.client.HTTPSConnection(
                "127.0.0.1",
                self.port,
                context=self.context,
                timeout=8,
                source_address=(source, 0),
            )
            if tls
            else http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=8)
        )
        try:
            conn.request(
                method,
                path,
                body=body,
                headers=headers or {"Content-Type": "application/cbor"},
            )
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    def raw(self, source="127.0.0.1"):
        return self.context.wrap_socket(
            socket.create_connection(
                ("127.0.0.1", self.port), timeout=8, source_address=(source, 0)
            ),
            server_hostname="127.0.0.1",
        )


@unittest.skipUnless(
    BINARY and shutil.which("openssl"),
    "set CUSTODIAN_OPENRESTY_BIN for isolated TLS integration",
)
class CustodianProxyTests(unittest.TestCase):
    def test_tls_prefix_headers_secret_handling_and_route_modes(self):
        for mode in ("custodian", "full"):
            with self.subTest(mode=mode), ProxyFixture(mode) as proxy:
                prefix = b"secret-body-never-log-or-spill-"
                marker = prefix + b"x" * (65536 - len(prefix))
                status, headers, body = proxy.request(
                    body=marker,
                    headers={
                        "Content-Type": "application/cbor",
                        "Authorization": "Bearer must-not-reach-custodian",
                        "X-Untrusted": "must-not-reach-custodian",
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, marker)
                self.assertEqual(headers["Cache-Control"], "no-store")
                path, forwarded, received = proxy.records[-1]
                self.assertEqual(path, "/v1/council")
                self.assertEqual(received, marker)
                self.assertEqual(forwarded["Connection"], "close")
                self.assertNotIn("Upgrade", forwarded)
                self.assertNotIn("Authorization", forwarded)
                self.assertNotIn("X-Untrusted", forwarded)
                for path in (
                    "/",
                    "/rpc",
                    "/summit",
                    "/ws",
                    "/prom-reth",
                    "/checkpointer",
                ):
                    status, _, _ = proxy.request(path=path)
                    self.assertEqual(
                        status,
                        404
                        if mode == "custodian"
                        else (401 if path in ("/prom-reth", "/checkpointer") else 200),
                    )
                for path in (
                    "/custodian",
                    "/custodian/",
                    "/custodian/v1/council/",
                    "/custodian/elsewhere",
                ):
                    self.assertEqual(proxy.request(path=path)[0], 404)
                for path in ("/custodian", "/custodian/v1/council"):
                    self.assertEqual(proxy.request(path=path, tls=False)[0], 404)
                for directory in ("client_body_temp", "proxy_temp"):
                    self.assertFalse(
                        any(p.is_file() for p in (proxy.root / directory).rglob("*"))
                    )
                for log in (proxy.root / "logs").glob("*.log"):
                    self.assertNotIn(
                        b"secret-body-never-log-or-spill", log.read_bytes()
                    )

    def test_rejections_never_reach_backend(self):
        cases = (
            ({"method": "GET"}, 405),
            ({"path": "/custodian/v1/council?secret=x"}, 400),
            ({"headers": {"Content-Type": "application/json"}}, 415),
            ({"body": b"x" * 65537}, 413),
            (
                {
                    "headers": {
                        "Content-Type": "application/cbor",
                        "Connection": "upgrade",
                    }
                },
                400,
            ),
            (
                {
                    "headers": {
                        "Content-Type": "application/cbor",
                        "Upgrade": "websocket",
                    }
                },
                400,
            ),
        )
        with ProxyFixture() as proxy:
            for kwargs, expected in cases:
                self.assertEqual(proxy.request(**kwargs)[0], expected)
            self.assertEqual(proxy.records, [])
            for framing in (b"", b"Transfer-Encoding: chunked\r\n"):
                with proxy.raw() as sock:
                    sock.sendall(
                        b"POST /custodian/v1/council HTTP/1.1\r\nHost: node.example.com\r\nContent-Type: application/cbor\r\n"
                        + framing
                        + b"\r\n"
                    )
                    self.assertIn(b" 400 " if framing else b" 411 ", sock.recv(4096))
            self.assertEqual(proxy.records, [])
            self.assertEqual(proxy.request()[0], 200)

    def test_absolute_upload_deadline_and_no_partial_forwarding(self):
        with ProxyFixture() as proxy, proxy.raw() as sock:
            sock.sendall(
                b"POST /custodian/v1/council HTTP/1.1\r\nHost: node.example.com\r\nContent-Type: application/cbor\r\nContent-Length: 65536\r\n\r\n"
            )
            started = time.monotonic()
            # Nonblocking SSL is essential: a readable fd can contain only a
            # TLS session ticket. A blocking recv would then stall the sender
            # and accidentally test an idle upload rather than continuous progress.
            sock.setblocking(False)
            response = None
            next_send = started
            pending = b""
            sent = 0
            while time.monotonic() - started < 7:
                if not pending and time.monotonic() >= next_send:
                    pending = b"s" * 4096
                    next_send += 0.5
                if pending:
                    try:
                        count = sock.send(pending)
                        sent += count
                        pending = pending[count:]
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        pass
                try:
                    response = sock.recv(4096)
                    break
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    time.sleep(0.02)
            self.assertGreaterEqual(
                sent, 8 * 4096, "test did not sustain upload progress"
            )
            self.assertIsNotNone(response, "drip-fed upload outlived its deadline")
            # nginx may close an incomplete request without emitting a 408 body.
            self.assertTrue(response == b"" or b" 408 " in response, response)
            self.assertLess(time.monotonic() - started, 6)
            self.assertEqual(proxy.records, [])
            self.assertEqual(proxy.request()[0], 200)
            for log in (proxy.root / "logs").glob("*.log"):
                self.assertNotIn(b"s" * 32, log.read_bytes())

    def test_connection_and_rate_limits(self):
        with ProxyFixture() as proxy:
            sockets = []
            try:
                for _ in range(4):
                    sock = proxy.raw()
                    sock.sendall(
                        b"POST /custodian/v1/council HTTP/1.1\r\nHost: node.example.com\r\nContent-Type: application/cbor\r\nContent-Length: 100\r\n\r\nx"
                    )
                    sockets.append(sock)
                time.sleep(0.1)
                self.assertEqual(proxy.request()[0], 429)
                self.assertEqual(proxy.records, [])
            finally:
                for sock in sockets:
                    sock.close()
        with ProxyFixture() as proxy:
            codes = [proxy.request()[0] for _ in range(30)]
            self.assertIn(200, codes)
            self.assertIn(429, codes)

    def test_global_limits_across_distinct_source_ips(self):
        with ProxyFixture() as proxy:
            sockets = []
            try:
                for index in range(16):
                    sock = proxy.raw(source=f"127.0.0.{index + 2}")
                    sock.sendall(
                        b"POST /custodian/v1/council HTTP/1.1\r\nHost: node.example.com\r\nContent-Type: application/cbor\r\nContent-Length: 100\r\n\r\nx"
                    )
                    sockets.append(sock)
                time.sleep(0.1)
                self.assertEqual(proxy.request(source="127.0.0.100")[0], 429)
                self.assertEqual(proxy.records, [])
            finally:
                for sock in sockets:
                    sock.close()
        with ProxyFixture() as proxy:
            codes = [
                proxy.request(source=f"127.0.0.{index + 2}")[0] for index in range(50)
            ]
            self.assertIn(200, codes)
            self.assertIn(429, codes)

    @unittest.skipUnless(
        os.environ.get("CUSTODIAN_SERVICE_BIN")
        and os.environ.get("COUNCIL_SIGNER_BIN"),
        "set CUSTODIAN_SERVICE_BIN and COUNCIL_SIGNER_BIN for real protocol smoke test",
    )
    def test_real_council_cli_and_custodian_through_verified_tls_tunnel(self):
        # The Rust client uses public certificate roots and has no custom-CA
        # option. Use its supported loopback tunnel mode: the local fixture
        # bridge verifies the TLS fixture CA. Also prove direct untrusted TLS fails.
        with tempfile.TemporaryDirectory(prefix="custodian-protocol-") as tmp:
            root = Path(tmp)
            port = free_port()
            process = subprocess.Popen(
                [
                    os.environ["CUSTODIAN_SERVICE_BIN"],
                    "--socket",
                    str(root / "custodian.sock"),
                    "--root-key-file",
                    str(root / "root.key"),
                    "--delivery-dir",
                    str(root / "deliveries"),
                    "--council-listen",
                    f"127.0.0.1:{port}",
                    "--council-address",
                    "0xd412c5ecd343e264381ff15afc0ad78a67b79f35",
                    "--chain-id",
                    "5124",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(100):
                    self.assertIsNone(process.poll(), "fixture Custodian exited")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("fixture Custodian did not start")
                with ProxyFixture(backend_port=port) as proxy:
                    rejected = subprocess.run(
                        [
                            os.environ["COUNCIL_SIGNER_BIN"],
                            "status",
                            "--node",
                            f"https://127.0.0.1:{proxy.port}/custodian",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(proxy.records, [])

                    class Tunnel(BaseHTTPRequestHandler):
                        def do_POST(self):
                            body = self.rfile.read(int(self.headers["Content-Length"]))
                            code, headers, body = proxy.request(
                                path=self.path,
                                body=body,
                                headers={"Content-Type": self.headers["Content-Type"]},
                            )
                            self.send_response(code)
                            self.send_header("Content-Type", headers["Content-Type"])
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)

                        def log_message(self, *args):
                            pass

                    tunnel = ThreadingHTTPServer(("127.0.0.1", 0), Tunnel)
                    thread = threading.Thread(target=tunnel.serve_forever, daemon=True)
                    thread.start()
                    try:
                        result = subprocess.run(
                            [
                                os.environ["COUNCIL_SIGNER_BIN"],
                                "status",
                                "--node",
                                f"http://127.0.0.1:{tunnel.server_port}/custodian",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(len(proxy.records), 1)
                        self.assertEqual(proxy.records[0][0], "/v1/council")
                    finally:
                        tunnel.shutdown()
                        tunnel.server_close()
                        thread.join(timeout=5)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

"""Opt-in real Prometheus 3.5 agent -> HTTPS fixture -> receiver integration.

All listeners use ephemeral loopback ports, all data is temporary, and only the
binary explicitly supplied in PROMETHEUS_AGENT_BIN is executed. No installer,
Supervisor or live validator is contacted.
"""

from __future__ import annotations

import configparser
import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_prometheus_agent import agent, settings

BINARY = os.environ.get("PROMETHEUS_AGENT_BIN")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(
    BINARY and shutil.which("openssl"),
    "set PROMETHEUS_AGENT_BIN for isolated integration",
)
class AgentIntegrationTests(unittest.TestCase):
    def test_forward_labels_authentication_and_failed_scrapes(self):
        received_auth = []
        exporter_ok = threading.Event()
        exporter_ok.set()
        receiver_port, agent_port = free_port(), free_port()

        class Exporter(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(
                    200 if exporter_ok.is_set() and self.path == "/" else 503
                )
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.end_headers()
                self.wfile.write(
                    b"# TYPE fixture_counter_total counter\nfixture_counter_total 1\n"
                )

            def log_message(self, *args):
                pass

        class Ingress(BaseHTTPRequestHandler):
            def do_POST(self):
                received_auth.append(self.headers.get("Authorization"))
                if (
                    self.path != "/api/v1/write"
                    or self.headers.get("Authorization") != "Bearer " + "a" * 64
                ):
                    self.send_error(401)
                    return
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                headers = {
                    key: self.headers[key]
                    for key in (
                        "Content-Type",
                        "Content-Encoding",
                        "X-Prometheus-Remote-Write-Version",
                    )
                    if key in self.headers
                }
                request = urllib.request.Request(
                    f"http://127.0.0.1:{receiver_port}/api/v1/write",
                    data=payload,
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        code = response.status
                except urllib.error.HTTPError as error:
                    code = error.code
                self.send_response(code)
                self.end_headers()

            def log_message(self, *args):
                pass

        def query(expression):
            query_string = urllib.parse.urlencode({"query": expression})
            with urllib.request.urlopen(
                f"http://127.0.0.1:{receiver_port}/api/v1/query?{query_string}",
                timeout=2,
            ) as response:
                return json.load(response)["data"]["result"]

        def wait_for(predicate):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return
                except (OSError, KeyError, urllib.error.URLError):
                    pass
                time.sleep(0.25)
            self.fail("Isolated agent/receiver did not reach the expected state")

        with tempfile.TemporaryDirectory(prefix="seismic-agent-integration-") as tmp:
            root = Path(tmp)
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                ],
                check=True,
                capture_output=True,
            )
            exporter = ThreadingHTTPServer(("127.0.0.1", 0), Exporter)
            ingress = ThreadingHTTPServer(("127.0.0.1", 0), Ingress)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(root / "cert.pem", root / "key.pem")
            ingress.socket = tls.wrap_socket(ingress.socket, server_side=True)
            threads = [
                threading.Thread(target=server.serve_forever, daemon=True)
                for server in (exporter, ingress)
            ]
            for thread in threads:
                thread.start()
            processes = []
            handles = []
            try:
                receiver_config = root / "receiver.yml"
                receiver_config.write_text("scrape_configs: []\n")
                commands = [
                    [
                        BINARY,
                        f"--config.file={receiver_config}",
                        f"--storage.tsdb.path={root / 'receiver-data'}",
                        f"--web.listen-address=127.0.0.1:{receiver_port}",
                        "--web.enable-remote-write-receiver",
                    ]
                ]
                token = root / "token"
                token.write_text("a" * 64)
                config, service = agent.render(settings(), token)
                # Production requires HTTPS:443; fixture transport is HTTPS on a
                # high loopback port with an explicitly trusted temporary CA.
                config = config.replace(
                    "https://metrics.example.com/api/v1/write",
                    f"https://127.0.0.1:{ingress.server_port}/api/v1/write",
                )
                for port in (9090, 9001):
                    config = config.replace(
                        f"127.0.0.1:{port}", f"127.0.0.1:{exporter.server_port}"
                    )
                config = config.replace("127.0.0.1:9091", f"127.0.0.1:{agent_port}")
                config = config.replace(
                    "scrape_interval: 15s", "scrape_interval: 1s"
                ).replace("scrape_timeout: 10s", "scrape_timeout: 1s")
                config = config.replace(
                    "    authorization:",
                    f"    tls_config:\n      ca_file: {root / 'cert.pem'}\n    authorization:",
                )
                config_path = root / "agent.yml"
                config_path.write_text(config)
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(service)
                command = shlex.split(parser["program:prometheus-agent"]["command"])
                command[0] = BINARY
                command = [
                    part.replace(str(agent.CONFIG), str(config_path))
                    .replace(settings()["data_dir"], str(root / "agent-data"))
                    .replace("127.0.0.1:9091", f"127.0.0.1:{agent_port}")
                    for part in command
                ]
                commands.append(command)
                for number, command in enumerate(commands):
                    output = (root / f"process-{number}.log").open("wb")
                    handles.append(output)
                    processes.append(
                        subprocess.Popen(
                            command, stdout=output, stderr=subprocess.STDOUT
                        )
                    )
                expression = 'up{node="validator-0.example.com"}'
                wait_for(lambda: len(query(expression)) == 3)
                result = query(expression)
                self.assertEqual(
                    {sample["metric"]["job"] for sample in result},
                    {
                        f"{component}/validator-0.example.com"
                        for component in ("summit", "reth", "prometheus-agent")
                    },
                )
                self.assertTrue(
                    all(
                        sample["metric"]["instance"] == "validator-0.example.com"
                        for sample in result
                    )
                )
                self.assertTrue(
                    all(sample["metric"]["role"] == "validator" for sample in result)
                )
                self.assertTrue(received_auth)
                self.assertEqual(set(received_auth), {"Bearer " + "a" * 64})
                exporter_ok.clear()
                wait_for(lambda: len(query(expression + " == 0")) == 2)
                self.assertEqual(
                    len(
                        query('up{job="prometheus-agent/validator-0.example.com"} == 1')
                    ),
                    1,
                )
            finally:
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=40)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                for output in handles:
                    output.close()
                for server in (exporter, ingress):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

# Installing the Prometheus Agent by hand

`seismic-node monitoring install` (runbook [3.12](RUNBOOK.md#312-monitoring)) is
the supported way to install the agent. This page is the same procedure as shell
commands, for operators who want to see or audit every step, or who cannot run
the CLI. The result must be byte-identical to what the CLI produces:
`seismic-node monitoring check` compares the installed files with what it would
render from `node.env`, and `validator start` runs that check before it starts
the agent.

Everything below runs as root on the node, with `node.env` already installed and
validated (runbook 3.6) and `MONITORING_MODE=push`. `$REPO` is the
`seismic-node-ops` checkout.

The pinned release and its checksums live in `tools/seismic_node/agent.py`
(`VERSION`, `CHECKSUMS`); the values below are copied from there and a test
keeps them in step.

## 1. Inputs

```bash
set -a; source <(sudo cat /etc/seismic/node.env); set +a
[[ "$MONITORING_MODE" == push ]] || echo "the agent is only used in push mode; stop here"
TOKEN_SOURCE=/root/monitoring-token          # the 64-hex token from Seismic operations, root-only
AGENT_HOME=/etc/seismic/prometheus-agent
RELEASES=/usr/local/lib/seismic/prometheus-agent
VERSION=3.5.0
ARCH=$(case "$(uname -m)" in x86_64) echo amd64 ;; aarch64) echo arm64 ;; *) echo unsupported ;; esac)
```

The CLI refuses a token source inside `RETH_DATA_DIR`, `SUMMIT_DATA_DIR` or
`KEYS_DIR`, a token that is not exactly 64 hexadecimal characters, and a handoff
file that is not `0600`. It also requires `MONITORING_DATA_DIR` to be outside
`/etc`, `/usr` and the two agent directories.

## 2. Service account

A dedicated account with a private primary group. The CLI refuses an existing
account whose group is shared with any other user.

```bash
id seismic-prometheus 2>/dev/null \
  || sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin seismic-prometheus
```

## 3. Directories

```bash
sudo install -d -o root -g seismic-prometheus -m 0750 "$AGENT_HOME"
sudo install -d -o seismic-prometheus -g seismic-prometheus -m 0750 "$MONITORING_DATA_DIR"
sudo install -d -o root -g root -m 0755 "$RELEASES" "$RELEASES/$VERSION"
```

`MONITORING_DATA_DIR` holds the agent's WAL. The CLI refuses to adopt a
non-empty directory on a first install.

## 4. Release binary

The official Prometheus release tarball, checked against the pinned SHA-256
before anything is extracted. Only the two executables are taken out of the
archive, as plain files with fixed permissions; archive paths, links and modes
are never applied to the host.

```bash
case "$ARCH" in
  amd64) SHA256=e811827af26d822afb09a4f28314f61b618b12cff5369835a67f674d8b46f39a ;;
  arm64) SHA256=173389cc42bf09c4e6e54cb53fa07a5a835d7c261e14775d2183181d6e385d1c ;;
esac
tmp=$(mktemp -d)
curl -fsSL --max-filesize 419430400 \
  -o "$tmp/prometheus.tar.gz" \
  "https://github.com/prometheus/prometheus/releases/download/v$VERSION/prometheus-$VERSION.linux-$ARCH.tar.gz"
echo "$SHA256  $tmp/prometheus.tar.gz" | sha256sum -c -
for name in prometheus promtool; do
  tar -xzf "$tmp/prometheus.tar.gz" -O "prometheus-$VERSION.linux-$ARCH/$name" > "$tmp/$name"
  sudo install -o root -g root -m 0755 "$tmp/$name" "$RELEASES/$VERSION/$name"
done
rm -rf "$tmp"
sudo ln -sfn "$VERSION" "$RELEASES/current"
"$RELEASES/current/prometheus" --version | head -1
```

The unit runs `$RELEASES/current/prometheus`; `current` is a relative symlink to
the version directory, so an upgrade is a new directory and a retargeted link.

## 5. Scrape targets

`targets.json` lists the node's loopback metrics endpoints. Summit's and Reth's
ports are fixed by their units; the Custodian serves `/metrics` on
`CUSTODIAN_COUNCIL_PORT`; the checkpointer entry exists only with
`CHECKPOINTER_ENABLED=true`.

```bash
{
  printf '[\n'
  printf '  {\n    "targets": [\n      "127.0.0.1:9090"\n    ],\n    "labels": {\n      "service": "summit",\n      "__metrics_path__": "/"\n    }\n  },\n'
  printf '  {\n    "targets": [\n      "127.0.0.1:9001"\n    ],\n    "labels": {\n      "service": "reth",\n      "__metrics_path__": "/"\n    }\n  },\n'
  printf '  {\n    "targets": [\n      "127.0.0.1:%s"\n    ],\n    "labels": {\n      "service": "custodian",\n      "__metrics_path__": "/metrics"\n    }\n  }' "$CUSTODIAN_COUNCIL_PORT"
  if [[ "$CHECKPOINTER_ENABLED" == true ]]; then
    printf ',\n  {\n    "targets": [\n      "127.0.0.1:42069"\n    ],\n    "labels": {\n      "service": "checkpointer",\n      "__metrics_path__": "/metrics"\n    }\n  }'
  fi
  printf '\n]\n'
} > /tmp/targets.json
python3 -m json.tool /tmp/targets.json >/dev/null && echo "targets.json is valid JSON"
```

## 6. Agent configuration

`prometheus.yml` is `install/templates/prometheus-agent.yml` with three
placeholders filled in: the node name, the role, and the `remote_write` list.
Validators push to Seismic with the bearer token; a second destination, the
operator's own Prometheus, is added when `MONITORING_OPERATOR_REMOTE_WRITE_URL`
is set (observers only have that one). Each destination has its own queue.

```bash
queue='    remote_timeout: 30s
    queue_config:
      min_shards: 1
      max_shards: 2
      capacity: 10000
      max_samples_per_send: 2000
      batch_send_deadline: 5s
      min_backoff: 1s
      max_backoff: 30s
      retry_on_http_429: true'
remote_writes=""
# Only validators push to Seismic; an observer's node.env may still carry the
# key, and the CLI ignores it there.
if [[ "$SEISMIC_ROLE" == validator && -n "${MONITORING_REMOTE_WRITE_URL:-}" ]]; then
  remote_writes+="  - name: seismic-monitoring
    url: \"$MONITORING_REMOTE_WRITE_URL\"
    authorization:
      type: Bearer
      credentials_file: \"$AGENT_HOME/token\"
$queue
"
fi
if [[ -n "${MONITORING_OPERATOR_REMOTE_WRITE_URL:-}" ]]; then
  remote_writes+="  - name: operator
    url: \"$MONITORING_OPERATOR_REMOTE_WRITE_URL\"
$queue
"
  # With a bearer token for your receiver, add under the url line, as above:
  #   authorization:
  #     type: Bearer
  #     credentials_file: "$AGENT_HOME/operator-token"
fi
python3 - "$REPO/install/templates/prometheus-agent.yml" "$MONITORING_NODE_NAME" "$SEISMIC_ROLE" "$remote_writes" > /tmp/prometheus.yml <<'EOF'
import sys
template, node, role, remote_writes = sys.argv[1:]
text = open(template).read()
text = text.replace("NODE_PLACEHOLDER", node).replace("ROLE_PLACEHOLDER", role)
text = text.replace("REMOTE_WRITE_PLACEHOLDER\n", remote_writes)
assert "_PLACEHOLDER" not in text, "unrendered placeholder"
sys.stdout.write(text)
EOF
"$RELEASES/current/promtool" check config --agent /tmp/prometheus.yml   # finds targets.json next to it
```

## 7. Token and files

Written with the agent group readable and nothing else; each file is created
complete and then moved into place.

```bash
sudo install -o root -g seismic-prometheus -m 0640 "$TOKEN_SOURCE" "$AGENT_HOME/token"
sudo install -o root -g seismic-prometheus -m 0640 /tmp/targets.json "$AGENT_HOME/targets.json"
sudo install -o root -g seismic-prometheus -m 0640 /tmp/prometheus.yml "$AGENT_HOME/prometheus.yml"
rm -f /tmp/targets.json /tmp/prometheus.yml
```

With an operator token, also install it as `$AGENT_HOME/operator-token` with the
same ownership and mode. A token file the configuration no longer references
must be removed, so no stale credential is left behind.

## 8. Verify

```bash
sudo "$REPO/tools/seismic-node.py" monitoring check
sudo systemd-analyze verify /etc/systemd/system/seismic-prometheus-agent.service
```

`check` re-renders both files from `node.env` and compares them with what is
installed, and confirms the binary, the token and the WAL directory. Any
difference means the manual steps diverged from the CLI; fix the difference
rather than the check. Nothing has been started: `validator start` (runbook
section 4) starts the agent once the node's services are up, or
`seismic-node monitoring start` does so on its own.

## Updating

Re-run the steps whose inputs changed. The agent must be stopped first
(`seismic-node monitoring stop`): the CLI refuses to replace the files of a
running agent, and the manual procedure should too, since the agent re-reads
nothing at runtime. A new Prometheus version is a new `$RELEASES/<version>`
directory and a retargeted `current` link; the old directory can be removed once
the agent runs on the new one.
